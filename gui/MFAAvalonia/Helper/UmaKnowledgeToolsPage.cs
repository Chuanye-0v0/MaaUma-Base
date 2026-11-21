using System;
using System.IO;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;

namespace MFAAvalonia.Helper;

/// <summary>Starts and verifies the loopback toolbox when its page is visited.</summary>
public sealed class UmaKnowledgeToolsPage : UserControl
{
    private readonly Func<UmaKnowledgeToolsLauncher> createLauncher;
    private readonly Func<Uri, Action<string>, Control> createBrowser;
    private readonly TextBlock status = new() { Name = "KnowledgeToolsStatus", TextWrapping = TextWrapping.Wrap };
    private readonly TextBox address = new() { Name = "KnowledgeToolsAddress", IsReadOnly = true,
        Watermark = "连接后显示本机地址", HorizontalAlignment = HorizontalAlignment.Stretch };
    private readonly Button retry = new() { Name = "KnowledgeToolsRetry", Content = "重试连接" };
    private readonly Button open = new() { Name = "KnowledgeToolsOpenBrowser", Content = "用浏览器打开", IsEnabled = false };
    private readonly Button copy = new() { Name = "KnowledgeToolsCopyAddress", Content = "复制地址", IsEnabled = false };
    private readonly ContentControl browserHost = new() { Name = "KnowledgeToolsEmbeddedBrowser",
        HorizontalContentAlignment = HorizontalAlignment.Stretch, VerticalContentAlignment = VerticalAlignment.Stretch };
    private UmaKnowledgeToolsLauncher? launcher;
    private bool visited;
    private bool busy;
    private bool ready;

    public UmaKnowledgeToolsPage() : this(() => new UmaKnowledgeToolsLauncher(), CreateBrowser) { }

    internal UmaKnowledgeToolsPage(Func<UmaKnowledgeToolsLauncher> createLauncher,
        Func<Uri, Action<string>, Control> createBrowser)
    {
        this.createLauncher = createLauncher;
        this.createBrowser = createBrowser;
        Name = "UmaKnowledgeToolsPage";
        var header = new StackPanel { Spacing = 8 };
        header.Children.Add(new TextBlock { Text = "资料工具箱", FontSize = 24,
            FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap });
        header.Children.Add(new TextBlock { Text = "查询技能、协助卡事件和六卡 SP 预算。带入和取回的资料以本机草稿保存。",
            TextWrapping = TextWrapping.Wrap });
        status.Text = "正在等待打开工具箱页面。";
        var actions = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var button in new[] { open, copy, retry })
        {
            button.Margin = new Thickness(0, 0, 8, 5);
            actions.Children.Add(button);
        }
        var body = new StackPanel { Spacing = 8, Children = { status, address, actions } };
        var card = new Border { Child = body, Padding = new Thickness(12), CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(1) };
        card.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
        header.Children.Add(card);
        var root = new Grid { RowDefinitions = new("Auto,*"), RowSpacing = 8,
            Margin = new Thickness(16, 10, 16, 16), Children = { header, browserHost } };
        Grid.SetRow(browserHost, 1);
        Content = root;

        Loaded += async (_, _) =>
        {
            if (!visited)
            {
                visited = true;
                await ConnectAsync();
            }
        };
        retry.Click += async (_, _) => await ConnectAsync();
        open.Click += async (_, _) => await OpenBrowserAsync();
        copy.Click += async (_, _) => await CopyAddressAsync();
    }

    private void UpdateButtons()
    {
        retry.IsEnabled = !busy;
        open.IsEnabled = copy.IsEnabled = ready && !busy;
    }

    private async Task ConnectAsync()
    {
        if (busy) return;
        busy = true;
        ready = false;
        UpdateButtons();
        status.Text = "正在连接本机资料工具箱…";
        try
        {
            var prior = browserHost.Content;
            browserHost.Content = null;
            if (prior is IDisposable disposable) disposable.Dispose();
            launcher ??= createLauncher();
            address.Text = launcher.BrowserUrl.AbsoluteUri;
            await launcher.EnsureReadyAsync();
            ready = true;
            ShowEmbedded(launcher.BrowserUrl);
        }
        catch (InvalidOperationException error)
        {
            status.Text = error.Message;
        }
        catch (JsonException)
        {
            status.Text = "资料工具箱启动配置无法读取。请核对工具箱文件后重试连接。";
        }
        catch
        {
            status.Text = "暂时无法连接资料工具箱。请重试连接。";
        }
        finally
        {
            busy = false;
            UpdateButtons();
        }
    }

    private void ShowEmbedded(Uri origin)
    {
        status.Text = "本机资料工具箱已连接，正在加载内嵌页面…";
        try
        {
            browserHost.Content = createBrowser(origin, text => status.Text = text);
        }
        catch
        {
            status.Text = "暂时无法显示内嵌工具箱。可以重试连接，或用系统浏览器打开。";
        }
    }

    private static Control CreateBrowser(Uri origin, Action<string> reportStatus)
    {
        var appData = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (string.IsNullOrWhiteSpace(appData))
            appData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var browser = UmaParentPlannerBrowser.ForLocalToolbox(origin, Path.Combine(appData, "MaaUma", "kt-web"));
        browser.StatusChanged += reportStatus;
        browser.Navigate(origin.AbsoluteUri);
        return browser;
    }

    private async Task OpenBrowserAsync()
    {
        if (busy || !ready || launcher is null) return;
        busy = true;
        UpdateButtons();
        status.Text = "正在用系统浏览器打开工具箱…";
        try
        {
            status.Text = await launcher.OpenAsync();
        }
        catch (InvalidOperationException error)
        {
            status.Text = error.Message;
        }
        catch
        {
            status.Text = "系统浏览器未能打开。可以复制地址，或重试连接后再打开。";
        }
        finally
        {
            busy = false;
            UpdateButtons();
        }
    }

    private async Task CopyAddressAsync()
    {
        if (!ready || launcher is null) return;
        try
        {
            if (TopLevel.GetTopLevel(this)?.Clipboard is not { } clipboard)
                throw new InvalidOperationException();
            await clipboard.SetTextAsync(launcher.BrowserUrl.AbsoluteUri);
            status.Text = "本机地址已复制。";
        }
        catch
        {
            status.Text = "暂时无法写入剪贴板，可选中上方地址手动复制。";
        }
    }
}
