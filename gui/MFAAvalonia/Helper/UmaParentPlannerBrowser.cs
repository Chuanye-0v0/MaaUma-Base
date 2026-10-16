using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Platform;
using Avalonia.Threading;
using Microsoft.Web.WebView2.Core;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>
/// One original-site browser. The owning page keeps one instance per site and
/// never gives the website a host object, message handler or local-library data.
/// </summary>
public sealed class UmaParentPlannerBrowser : NativeControlHost, IDisposable
{
    private readonly Uri allowedOrigin;
    private readonly string profile;
    private CoreWebView2Controller? controller;
    private IntPtr hostWindow;
    private IntPtr parkingWindow;
    private Window? ownerWindow;
    private string? pendingUrl;
    private bool initializing;
    private bool disposed;
    private bool documentReady;
    private bool lostPageState;
    private int lifetime;
    private long navigationVersion;

    public event Action<string>? StatusChanged;

    public string? CurrentUrl
    {
        get
        {
            if (disposed) return null;
            if (controller == null) return pendingUrl;
            try { return controller.CoreWebView2.Source; }
            catch (Exception ex) when (ex is COMException or InvalidOperationException) { return null; }
        }
    }

    /// <param name="allowedOrigin">http://umadna.com or https://614tools.top.</param>
    /// <param name="profile">An app-owned, dedicated WebView2 user-data directory.</param>
    public UmaParentPlannerBrowser(string allowedOrigin, string profile)
    {
        this.allowedOrigin = ValidateOrigin(allowedOrigin);
        if (string.IsNullOrWhiteSpace(profile)) throw new ArgumentException("浏览器资料目录不能为空。", nameof(profile));
        this.profile = Path.GetFullPath(profile);
        MinHeight = 320;
        SizeChanged += (_, _) => ResizeBrowser();
        PropertyChanged += (_, e) =>
        {
            if (e.Property == IsVisibleProperty) ResizeBrowser();
        };
    }

    public void Navigate(string url)
    {
        VerifyUsable();
        if (!IsAllowedUrl(allowedOrigin, url))
            throw new ArgumentException("仅支持该工具的作者原站地址。", nameof(url));
        pendingUrl = url;
        documentReady = false;
        ++navigationVersion;
        if (controller != null) controller.CoreWebView2.Navigate(url);
        else if (hostWindow != IntPtr.Zero && !initializing) _ = InitializeAsync();
    }

    // The caller supplies a fixed, reviewed C# script, never website/user text.
    // This is deliberately internal and is not exposed through a web bridge.
    internal async Task<JArray> EvaluateReadOnlyTargetsAsync(string trustedScript)
    {
        VerifyUsable();
        if (allowedOrigin.Scheme != "http" || allowedOrigin.Host != "umadna.com")
            throw new InvalidOperationException("当前工具不支持读取规划目标。");
        if (string.IsNullOrWhiteSpace(trustedScript)) throw new ArgumentException("缺少目标读取脚本。", nameof(trustedScript));
        var current = controller;
        if (current == null || !documentReady || !IsAllowedUrl(allowedOrigin, current.CoreWebView2.Source))
            throw new InvalidOperationException("请先等待种马规划网页加载完成。");
        var version = navigationVersion;
        var source = current.CoreWebView2.Source;
        // Recheck inside the document as ExecuteScriptAsync may run after a navigation.
        var script = "(() => { if (location.origin !== " +
            JsonConvert.SerializeObject(allowedOrigin.GetLeftPart(UriPartial.Authority)) +
            ") return null; return (" + trustedScript + "); })()";
        string json;
        try { json = await current.CoreWebView2.ExecuteScriptAsync(script).WaitAsync(TimeSpan.FromSeconds(10)); }
        catch (TimeoutException)
        {
            throw new InvalidOperationException("读取网页目标超时，请等待页面恢复后再试。");
        }
        if (disposed || controller != current || version != navigationVersion || !documentReady ||
            !string.Equals(source, current.CoreWebView2.Source, StringComparison.Ordinal))
            throw new InvalidOperationException("读取时网页发生了切换，请在当前页面重新读取目标。");
        return ParseTargets(json);
    }

    internal static JArray ParseTargets(string json)
    {
        if (string.IsNullOrWhiteSpace(json) || json.Length > 65536)
            throw new InvalidOperationException("网页目标内容为空或过大，请检查作者原站。");
        try
        {
            if (JToken.Parse(json) is JArray rows && rows.Count <= 256) return rows;
        }
        catch (JsonException) { }
        throw new InvalidOperationException("暂时无法识别网页中的目标，请检查页面或手动填写。");
    }

    internal static Uri ValidateOrigin(string value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || !uri.IsDefaultPort ||
            uri.UserInfo.Length != 0 || uri.AbsolutePath != "/" || uri.Query.Length != 0 || uri.Fragment.Length != 0 ||
            !((uri.Scheme == "http" && string.Equals(uri.Host, "umadna.com", StringComparison.OrdinalIgnoreCase)) ||
              (uri.Scheme == "https" && string.Equals(uri.Host, "614tools.top", StringComparison.OrdinalIgnoreCase))))
            throw new ArgumentException("浏览器仅支持已登记的作者原站。", nameof(value));
        return uri;
    }

    internal static bool IsAllowedUrl(Uri origin, string value) =>
        Uri.TryCreate(value, UriKind.Absolute, out var uri) && uri.UserInfo.Length == 0 && uri.IsDefaultPort &&
        uri.Scheme == origin.Scheme && string.Equals(uri.Host, origin.Host, StringComparison.OrdinalIgnoreCase) &&
        uri.Port == origin.Port;

    protected override void OnAttachedToVisualTree(VisualTreeAttachmentEventArgs e)
    {
        base.OnAttachedToVisualTree(e);
        ObserveOwnerWindow();
    }

    private void ObserveOwnerWindow()
    {
        if (TopLevel.GetTopLevel(this) is not Window owner || ReferenceEquals(owner, ownerWindow)) return;
        if (ownerWindow != null) ownerWindow.Closed -= OwnerClosed;
        ownerWindow = owner;
        ownerWindow.Closed += OwnerClosed;
    }

    private void OwnerClosed(object? sender, EventArgs e) => Dispose();

    protected override IPlatformHandle CreateNativeControlCore(IPlatformHandle parent)
    {
        VerifyUsable();
        if (!OperatingSystem.IsWindows() || parent.HandleDescriptor != "HWND")
            return base.CreateNativeControlCore(parent);
        ObserveOwnerWindow();
        EnsureParkingWindow();
        hostWindow = CreateWindowEx(0, "STATIC", "", 0x40000000 | 0x10000000 | 0x04000000,
            0, 0, 1, 1, parent.Handle, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
        if (hostWindow == IntPtr.Zero) throw new InvalidOperationException("无法创建网页显示区域。");
        if (controller != null)
        {
            try
            {
                controller.ParentWindow = hostWindow;
                controller.NotifyParentWindowPositionChanged();
                ResizeBrowser();
            }
            catch (Exception ex) when (ex is COMException or InvalidOperationException)
            {
                DropController();
                lostPageState = true;
                StatusChanged?.Invoke("网页显示恢复失败，需要重新加载；未保存的网页输入可能丢失。");
            }
        }
        if (controller == null && !initializing) _ = InitializeAsync();
        return new PlatformHandle(hostWindow, "HWND");
    }

    private void EnsureParkingWindow()
    {
        if (parkingWindow != IntPtr.Zero) return;
        // A hidden popup is independent of Avalonia's short-lived native host.
        // Keep it alive across page detach/attach; never show it or give it focus.
        parkingWindow = CreateWindowEx(0x00000080 | 0x08000000, "STATIC", "", unchecked((int)0x80000000),
            0, 0, 1, 1, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
        if (parkingWindow == IntPtr.Zero) throw new InvalidOperationException("无法准备网页切换区域。");
    }

    private async Task InitializeAsync()
    {
        if (disposed || initializing || controller != null) return;
        initializing = true;
        var version = lifetime;
        CoreWebView2Controller? created = null;
        try
        {
            EnsureParkingWindow();
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: profile);
            if (disposed || version != lifetime) return;
            // Creation also uses the parking window: detaching while this await is
            // pending cannot destroy its parent HWND.
            created = await environment.CreateCoreWebView2ControllerAsync(parkingWindow);
            if (disposed || version != lifetime) { created.Close(); return; }
            created.IsVisible = false;
            ConfigureBrowser(created.CoreWebView2);
            controller = created;
            if (hostWindow != IntPtr.Zero) created.ParentWindow = hostWindow;
            ResizeBrowser();
            if (pendingUrl != null) created.CoreWebView2.Navigate(pendingUrl);
        }
        catch (Exception)
        {
            if (created != null) { try { created.Close(); } catch (COMException) { } }
            if (!disposed && version == lifetime)
            {
                controller = null;
                documentReady = false;
                StatusChanged?.Invoke("内嵌网页加载失败，请重试或使用“作者原站”入口。");
            }
        }
        finally { if (version == lifetime) initializing = false; }
    }

    private void ConfigureBrowser(CoreWebView2 web)
    {
        web.Settings.IsPasswordAutosaveEnabled = false;
        web.Settings.IsGeneralAutofillEnabled = false;
        web.Settings.AreDevToolsEnabled = false;
        web.Settings.AreHostObjectsAllowed = false;
        web.Settings.IsWebMessageEnabled = false;
        web.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
        web.BasicAuthenticationRequested += (_, e) => e.Cancel = true;
        web.ClientCertificateRequested += (_, e) => { e.Cancel = true; e.Handled = true; };
        web.NavigationStarting += (_, e) =>
        {
            if (!IsAllowedUrl(allowedOrigin, e.Uri))
            {
                e.Cancel = true;
                StatusChanged?.Invoke("站外跳转未自动打开，可使用“作者原站”入口。");
                return;
            }
            documentReady = false;
            ++navigationVersion;
            pendingUrl = e.Uri;
        };
        web.FrameNavigationStarting += (_, e) =>
        {
            if (e.Uri != "about:blank" && !IsAllowedUrl(allowedOrigin, e.Uri)) e.Cancel = true;
        };
        web.LaunchingExternalUriScheme += (_, e) => e.Cancel = true;
        web.NewWindowRequested += (_, e) =>
        {
            e.Handled = true;
            StatusChanged?.Invoke("网页新窗口未自动打开，可使用“作者原站”入口。");
        };
        web.DownloadStarting += (_, e) =>
        {
            e.Cancel = true;
            StatusChanged?.Invoke("内嵌页面不下载文件；需要导出时请打开作者原站。");
        };
        web.NavigationCompleted += (_, e) =>
        {
            documentReady = e.IsSuccess && controller != null && IsAllowedUrl(allowedOrigin, controller.CoreWebView2.Source);
            if (documentReady)
            {
                StatusChanged?.Invoke(lostPageState ? "网页已重新加载，请检查未保存的输入。" : "作者原站 · " + allowedOrigin.Host);
                lostPageState = false;
            }
            else StatusChanged?.Invoke("网页加载未完成，可稍后重试或打开作者原站。");
        };
        web.ProcessFailed += (_, _) =>
        {
            documentReady = false;
            ++navigationVersion;
            StatusChanged?.Invoke("网页进程异常，请重新打开作者原站；未保存的输入可能丢失。");
        };
    }

    private void ResizeBrowser()
    {
        if (disposed || controller == null) return;
        var scale = TopLevel.GetTopLevel(this)?.RenderScaling ?? 1;
        try
        {
            controller.Bounds = new Rectangle(0, 0, Math.Max(1, (int)(Bounds.Width * scale)), Math.Max(1, (int)(Bounds.Height * scale)));
            controller.IsVisible = hostWindow != IntPtr.Zero && IsVisible;
        }
        catch (Exception ex) when (ex is COMException or InvalidOperationException)
        {
            documentReady = false;
            StatusChanged?.Invoke("网页显示暂不可用，请重新打开作者原站。");
        }
    }

    protected override void DestroyNativeControlCore(IPlatformHandle control)
    {
        if (hostWindow == IntPtr.Zero || control.Handle != hostWindow)
        {
            base.DestroyNativeControlCore(control);
            return;
        }
        if (!disposed && controller != null)
        {
            try
            {
                controller.IsVisible = false;
                controller.ParentWindow = parkingWindow;
                controller.NotifyParentWindowPositionChanged();
            }
            catch (Exception ex) when (ex is COMException or InvalidOperationException)
            {
                DropController();
                lostPageState = true;
                StatusChanged?.Invoke("网页未能保留当前输入，下次进入时会重新加载。");
            }
        }
        DestroyWindow(hostWindow);
        hostWindow = IntPtr.Zero;
    }

    private void DropController()
    {
        documentReady = false;
        ++navigationVersion;
        var old = controller;
        controller = null;
        if (old == null) return;
        try { old.Close(); } catch (Exception ex) when (ex is COMException or InvalidOperationException) { }
    }

    private void VerifyUsable()
    {
        Dispatcher.UIThread.VerifyAccess();
        ObjectDisposedException.ThrowIf(disposed, this);
    }

    public void Dispose()
    {
        Dispatcher.UIThread.VerifyAccess();
        if (disposed) return;
        disposed = true;
        ++lifetime;
        if (ownerWindow != null) ownerWindow.Closed -= OwnerClosed;
        ownerWindow = null;
        DropController();
        if (parkingWindow != IntPtr.Zero) { DestroyWindow(parkingWindow); parkingWindow = IntPtr.Zero; }
        // Avalonia owns hostWindow's detach/destruction; do not destroy it twice.
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateWindowEx(int extended, string className, string name, int style,
        int x, int y, int width, int height, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr parameter);
    [DllImport("user32.dll")]
    private static extern bool DestroyWindow(IntPtr hwnd);
}
