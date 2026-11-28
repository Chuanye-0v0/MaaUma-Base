using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
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
    private readonly bool maliMode;
    private CoreWebView2Environment? environment;
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
    private string? lastInitError;
    private bool IsToolbox => !maliMode && allowedOrigin.Scheme == Uri.UriSchemeHttp;
    private bool IsMali => maliMode;

    internal static string? BuildBrowserArguments(bool isToolbox, string? baseArgs)
    {
        const string loopbackNoProxyArgs = "--no-proxy-server --proxy-bypass-list=<-loopback>";
        if (!isToolbox)
            return string.IsNullOrWhiteSpace(baseArgs) ? null : baseArgs;
        return string.IsNullOrWhiteSpace(baseArgs)
            ? loopbackNoProxyArgs
            : loopbackNoProxyArgs + " " + baseArgs;
    }

    private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WNDCLASS
    {
        public uint style;
        public IntPtr lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public IntPtr hInstance;
        public IntPtr hIcon;
        public IntPtr hCursor;
        public IntPtr hbrBackground;
        public string? lpszMenuName;
        public string lpszClassName;
    }
    private const string HostWindowClassName = "MaaUmaWebView2Host";
    private static readonly WndProcDelegate DefWndProcRef = DefWindowProcW;
    private static readonly string ResolvedHostClass = RegisterHostClass();

    private static string RegisterHostClass()
    {
        if (!OperatingSystem.IsWindows()) return "STATIC";
        try
        {
            var u32 = GetModuleHandleW("user32.dll");
            if (u32 == IntPtr.Zero) u32 = LoadLibraryW("user32.dll");
            var defProc = u32 != IntPtr.Zero ? GetProcAddress(u32, "DefWindowProcW") : IntPtr.Zero;
            if (defProc == IntPtr.Zero) defProc = Marshal.GetFunctionPointerForDelegate(DefWndProcRef);

            var wc = new WNDCLASS
            {
                style = 0x0008,
                lpfnWndProc = defProc,
                hInstance = GetModuleHandleW(null),
                hbrBackground = (IntPtr)6, // COLOR_WINDOW + 1
                lpszClassName = HostWindowClassName,
            };
            var atom = RegisterClassW(ref wc);
            return (atom != 0 || Marshal.GetLastWin32Error() == 1410) ? HostWindowClassName : "STATIC";
        }
        catch { return "STATIC"; }
    }

    internal string? LastInitError => lastInitError;
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

    /// <param name="allowedOrigin">https://umadna.com or https://614tools.top.</param>
    /// <param name="profile">An app-owned, dedicated WebView2 user-data directory.</param>
    public UmaParentPlannerBrowser(string allowedOrigin, string profile) : this(ValidateOrigin(allowedOrigin), profile, maliMode: false) { }

    internal static UmaParentPlannerBrowser ForLocalToolbox(Uri origin, string profile)
    {
        if (!IsToolboxOrigin(origin))
            throw new ArgumentException("内嵌工具箱只支持已验证的本机端口。", nameof(origin));
        return new UmaParentPlannerBrowser(origin, profile, maliMode: false) { MinHeight = 0 };
    }

    internal static UmaParentPlannerBrowser ForMaliTool(string profile) =>
        new(new Uri("https://game.bilibili.com/"), profile, maliMode: true) { MinHeight = 420 };

    private UmaParentPlannerBrowser(Uri allowedOrigin, string profile) : this(allowedOrigin, profile, maliMode: false) { }

    private UmaParentPlannerBrowser(Uri allowedOrigin, string profile, bool maliMode)
    {
        this.allowedOrigin = allowedOrigin;
        this.maliMode = maliMode;
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
        var allowed = IsMali ? IsAllowedMaliUrl(url, isSubframe: false) : IsAllowedUrl(allowedOrigin, url);
        if (!allowed)
            throw new ArgumentException(
                IsMali ? "仅支持吗哩吗哩官网及 B 站官方登录地址。"
                    : IsToolbox ? "仅支持当前本机工具箱地址。" : "仅支持该工具的作者原站地址。",
                nameof(url));
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
        if (allowedOrigin.Scheme != "https" || allowedOrigin.Host != "umadna.com")
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

    internal async Task<JObject> ExecuteMaliJobAsync(string trustedAsyncFunctionJs, JObject payload, TimeSpan timeout)
    {
        VerifyUsable();
        if (!IsMali)
            throw new InvalidOperationException("当前浏览器实例未处于吗哩吗哩模式。");
        if (string.IsNullOrWhiteSpace(trustedAsyncFunctionJs))
            throw new ArgumentException("缺少检索执行脚本。", nameof(trustedAsyncFunctionJs));
        var current = controller;
        if (current == null && !string.IsNullOrWhiteSpace(lastInitError))
            throw new InvalidOperationException($"内嵌浏览器未就绪（{lastInitError}）。可点上方“系统浏览器打开 ↗”并在下方备选导入框粘贴结果。");
        if (current == null || !documentReady || !IsAllowedUrl(allowedOrigin, current.CoreWebView2.Source))
            throw new InvalidOperationException("请先回到吗哩吗哩官网（game.bilibili.com/tool/pd/）并等待页面加载完成。");

        var clamped = timeout < TimeSpan.FromSeconds(3) ? TimeSpan.FromSeconds(3)
            : timeout > TimeSpan.FromSeconds(25) ? TimeSpan.FromSeconds(25)
            : timeout;
        var version = navigationVersion;
        var source = current.CoreWebView2.Source;
        var jobId = Guid.NewGuid().ToString("N");
        var payloadJsonLiteral = JsonConvert.SerializeObject(payload.ToString(Formatting.None));
        var jobIdLiteral = JsonConvert.SerializeObject(jobId);

        var kickScript = $$"""
        (() => {
            if (location.origin !== "https://game.bilibili.com") {
                return { started: false, rejected: "wrong_origin" };
            }
            const jobs = window.__maaumaMaliJobs || (window.__maaumaMaliJobs = Object.create(null));
            const id = {{jobIdLiteral}};
            const payload = JSON.parse({{payloadJsonLiteral}});
            const fn = ({{trustedAsyncFunctionJs}});
            jobs[id] = { done: false, ok: false, data: null, error: null };
            Promise.resolve().then(() => fn(payload)).then(
                (data) => {
                    jobs[id] = { done: true, ok: true, data: data ?? null, error: null };
                },
                (err) => {
                    const errObj = err && typeof err === "object"
                        ? {
                            message: String(err.message || err),
                            status: Number(err.status) || 0,
                            code: Number(err.code) || 0,
                            riskControl: err.riskControl === true
                          }
                        : { message: String(err || "未知错误"), status: 0, code: 0, riskControl: false };
                    jobs[id] = { done: true, ok: false, data: null, error: errObj };
                }
            );
            return { started: true, jobId: id };
        })()
        """;

        string kickJson;
        try
        {
            kickJson = await current.CoreWebView2.ExecuteScriptAsync(kickScript).WaitAsync(TimeSpan.FromSeconds(5));
        }
        catch (TimeoutException)
        {
            throw new InvalidOperationException("向吗哩吗哩页面发起检索超时，请确认页面未卡死后重试。");
        }
        catch (Exception ex) when (ex is COMException or InvalidOperationException)
        {
            throw new InvalidOperationException("向吗哩吗哩页面发起检索失败，请重新加载页面后重试。");
        }

        if (string.IsNullOrWhiteSpace(kickJson) || kickJson == "null")
            throw new InvalidOperationException("吗哩吗哩页面尚未就绪，请等待页面加载完成后重试。");
        var kickToken = JToken.Parse(kickJson) as JObject;
        if (kickToken?["rejected"] != null)
            throw new InvalidOperationException("当前页面不是吗哩吗哩官网（game.bilibili.com），请先点『回到吗哩页面』。");

        var pollScript = $$"""
        (() => {
            const jobs = window.__maaumaMaliJobs;
            const id = {{jobIdLiteral}};
            const rec = jobs && jobs[id];
            if (!rec) return { missing: true };
            if (!rec.done) return { done: false };
            delete jobs[id];
            return rec;
        })()
        """;

        var sw = Stopwatch.StartNew();
        while (sw.Elapsed < clamped)
        {
            await Task.Delay(250);
            if (disposed || controller != current || version != navigationVersion || !documentReady ||
                !string.Equals(source, current.CoreWebView2.Source, StringComparison.Ordinal))
                throw new InvalidOperationException("检索期间页面发生跳转，请留在吗哩页面后重试。");

            string pollJson;
            try
            {
                pollJson = await current.CoreWebView2.ExecuteScriptAsync(pollScript).WaitAsync(TimeSpan.FromSeconds(5));
            }
            catch (TimeoutException)
            {
                continue;
            }
            catch (Exception ex) when (ex is COMException or InvalidOperationException)
            {
                throw new InvalidOperationException("检索期间页面连接中断，请留在吗哩页面后重试。");
            }

            if (string.IsNullOrWhiteSpace(pollJson) || pollJson == "null") continue;
            if (pollJson.Length > 2_000_000)
                throw new InvalidOperationException("吗哩吗哩返回数据过大，请减少页数后重试。");

            if (JToken.Parse(pollJson) is not JObject pollObj) continue;
            if ((bool?)pollObj["missing"] == true)
                throw new InvalidOperationException("检索状态在页面刷新后丢失，请重新点击检索。");
            if ((bool?)pollObj["done"] == true)
                return pollObj;
        }

        try
        {
            _ = current.CoreWebView2.ExecuteScriptAsync($"(() => {{ if (window.__maaumaMaliJobs) delete window.__maaumaMaliJobs[{jobIdLiteral}]; }})()");
        }
        catch { }
        throw new InvalidOperationException("吗哩吗哩接口检索超时，请稍后重试。");
    }

    internal static JArray ParseTargets(string json)
    {
        if (string.IsNullOrWhiteSpace(json) || json.Length > 65536)
            throw new InvalidOperationException("网页目标内容为空或过大，请检查作者原站。");
        try
        {
            var value=JToken.Parse(json);
            if(value is JObject unsupported && (string?)unsupported["unsupported"]=="target_selector_missing")
                throw new InvalidOperationException("网站已改版，当前网页不支持读取目标。请复制名称到本地规划添加。");
            if (value is JArray rows && rows.Count <= 256) return rows;
        }
        catch (JsonException) { }
        throw new InvalidOperationException("暂时无法识别网页中的目标，请检查页面或手动填写。");
    }

    internal static Uri ValidateOrigin(string value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || !uri.IsDefaultPort ||
            uri.UserInfo.Length != 0 || uri.AbsolutePath != "/" || uri.Query.Length != 0 || uri.Fragment.Length != 0 ||
            !((uri.Scheme == "https" && string.Equals(uri.Host, "umadna.com", StringComparison.OrdinalIgnoreCase)) ||
              (uri.Scheme == "https" && string.Equals(uri.Host, "614tools.top", StringComparison.OrdinalIgnoreCase))))
            throw new ArgumentException("浏览器仅支持已登记的作者原站。", nameof(value));
        return uri;
    }

    private static bool IsToolboxOrigin(Uri origin) => origin.IsAbsoluteUri && origin.Scheme == Uri.UriSchemeHttp &&
        origin.Port is 18765 or 18766 && origin.OriginalString == $"http://127.0.0.1:{origin.Port}/";

    internal static bool IsAllowedUrl(Uri origin, string value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || uri.UserInfo.Length != 0 ||
            uri.Scheme != origin.Scheme || !string.Equals(uri.Host, origin.Host, StringComparison.OrdinalIgnoreCase) ||
            uri.Port != origin.Port)
            return false;
        if (origin.Scheme == Uri.UriSchemeHttp)
            return IsToolboxOrigin(origin) && value.StartsWith(origin.OriginalString, StringComparison.Ordinal);
        return origin.Scheme == Uri.UriSchemeHttps && uri.IsDefaultPort;
    }

    internal static bool IsAllowedMaliUrl(string? value, bool isSubframe)
    {
        if (string.IsNullOrWhiteSpace(value)) return false;
        if (isSubframe && string.Equals(value, "about:blank", StringComparison.Ordinal)) return true;
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || uri.UserInfo.Length != 0 ||
            uri.Scheme != Uri.UriSchemeHttps || !uri.IsDefaultPort)
            return false;
        var host = uri.Host.ToLowerInvariant();
        var topAllowed = host is "game.bilibili.com" or "passport.bilibili.com" or "account.bilibili.com" or "www.bilibili.com" or "passport.biligame.com"
            || host.EndsWith(".biligame.com", StringComparison.Ordinal);
        if (!isSubframe) return topAllowed;
        return topAllowed
            || host == "bilibili.com" || host.EndsWith(".bilibili.com", StringComparison.Ordinal)
            || host.EndsWith(".hdslb.com", StringComparison.Ordinal)
            || host.EndsWith(".biliapi.net", StringComparison.Ordinal)
            || host == "geetest.com" || host.EndsWith(".geetest.com", StringComparison.Ordinal)
            || host.EndsWith(".geevisit.com", StringComparison.Ordinal)
            || host.EndsWith(".gtimg.com", StringComparison.Ordinal)
            || host.EndsWith(".gtimg.cn", StringComparison.Ordinal);
    }

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
        var scale = TopLevel.GetTopLevel(this)?.RenderScaling ?? 1.0;
        var initW = Bounds.Width >= 16 ? Math.Max(1, (int)(Bounds.Width * scale)) : 800;
        var initH = Bounds.Height >= 16 ? Math.Max(1, (int)(Bounds.Height * scale)) : 500;
        var hInst = GetModuleHandleW(null);
        // WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS
        hostWindow = CreateWindowEx(0, ResolvedHostClass, "", 0x40000000 | 0x10000000 | 0x02000000 | 0x04000000,
            0, 0, initW, initH, parent.Handle, IntPtr.Zero, hInst, IntPtr.Zero);
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
        if (controller == null && !initializing)
        {
            Dispatcher.UIThread.Post(() =>
            {
                if (!disposed && controller == null && !initializing && hostWindow != IntPtr.Zero)
                    _ = InitializeAsync();
            }, DispatcherPriority.Loaded);
        }
        return new PlatformHandle(hostWindow, "HWND");
    }

    private void EnsureParkingWindow()
    {
        if (parkingWindow != IntPtr.Zero) return;
        // Dedicated hidden overlapped window used only as a temporary detach park across tab switches.
        parkingWindow = CreateWindowEx(0, ResolvedHostClass, "", 0x00000000,
            0, 0, 1, 1, IntPtr.Zero, IntPtr.Zero, GetModuleHandleW(null), IntPtr.Zero);
        if (parkingWindow == IntPtr.Zero) throw new InvalidOperationException("无法准备网页切换区域。");
    }

    private async Task InitializeAsync()
    {
        if (disposed || initializing || controller != null) return;
        initializing = true;
        lastInitError = null;
        var version = lifetime;
        CoreWebView2Controller? created = null;
        string? origTemp = Environment.GetEnvironmentVariable("TEMP");
        string? origTmp = Environment.GetEnvironmentVariable("TMP");
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var safeTemp = Path.Combine(localAppData, "Temp");
        var diagPath = Path.Combine(localAppData, "MaaUma", "web-tools", "webview2-diag.log");

        try
        {
            try { OleInitialize(IntPtr.Zero); } catch { }
            EnsureParkingWindow();

            // Wait briefly for Avalonia Win32NativeControlHost to finish reparenting and showing the host HWND.
            for (var wait = 0; wait < 6; wait++)
            {
                if (disposed || version != lifetime) return;
                if (hostWindow != IntPtr.Zero && IsWindowVisible(GetAncestor(hostWindow, 2))) break;
                await Task.Delay(30);
            }
            if (disposed || version != lifetime) return;

            var swFlags = "--disable-gpu --disable-gpu-compositing --disable-direct-composition --disable-features=RendererCodeIntegrity --enable-logging --v=1";
            var attempts = new (string Label, string ProfileDir, string? ExtraArgs, bool UseParking)[]
            {
                ("standard", profile, BuildBrowserArguments(IsToolbox, null), false),
                ("software-clean", profile + "-sw", BuildBrowserArguments(IsToolbox, swFlags), false),
                ("parking-reparent", profile + "-sw2", BuildBrowserArguments(IsToolbox, swFlags), true),
            };

            var attemptLogs = new List<string>(attempts.Length);
            Exception? lastEx = null;
            var lastStep = "Init";
            var succeeded = false;

            try
            {
                if (Directory.Exists(safeTemp))
                {
                    if (!string.Equals(origTemp, safeTemp, StringComparison.OrdinalIgnoreCase))
                        Environment.SetEnvironmentVariable("TEMP", safeTemp);
                    if (!string.Equals(origTmp, safeTemp, StringComparison.OrdinalIgnoreCase))
                        Environment.SetEnvironmentVariable("TMP", safeTemp);
                }

                for (var idx = 0; idx < attempts.Length; idx++)
                {
                    var attempt = attempts[idx];
                    var step = "CreateAsync";
                    string? browserVer = null;
                    created = null;
                    try
                    {
                        Directory.CreateDirectory(attempt.ProfileDir);
                        var opts = string.IsNullOrEmpty(attempt.ExtraArgs)
                            ? null
                            : new CoreWebView2EnvironmentOptions(attempt.ExtraArgs);
                        var env = await CoreWebView2Environment.CreateAsync(null, attempt.ProfileDir, opts);
                        if (disposed || version != lifetime) return;
                        browserVer = env.BrowserVersionString;

                        step = "CreateController";
                        var createParent = attempt.UseParking
                            ? parkingWindow
                            : (hostWindow != IntPtr.Zero ? hostWindow : parkingWindow);
                        created = await env.CreateCoreWebView2ControllerAsync(createParent);
                        if (disposed || version != lifetime)
                        {
                            try { created.Close(); } catch (COMException) { }
                            created = null;
                            return;
                        }

                        if (hostWindow != IntPtr.Zero && createParent != hostWindow)
                        {
                            step = "Reparent";
                            created.ParentWindow = hostWindow;
                        }

                        step = "SetVisible";
                        created.IsVisible = hostWindow != IntPtr.Zero && IsVisible;

                        step = "ConfigureBrowser";
                        ConfigureBrowser(created.CoreWebView2);

                        environment = env;
                        controller = created;
                        created = null;

                        ResizeBrowser();
                        try { controller.NotifyParentWindowPositionChanged(); } catch { }
                        Dispatcher.UIThread.Post(ResizeBrowser);

                        step = "Navigate";
                        if (pendingUrl != null) controller.CoreWebView2.Navigate(pendingUrl);

                        attemptLogs.Add($"{attempt.Label}[ok ver={browserVer ?? "-"}]");
                        if (idx > 0 && attemptLogs.Count > 1)
                        {
                            AppendDiagLog(diagPath,
                                $"[{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss zzz}] [OK-FALLBACK] profile={profile} used={attempt.Label} ver={browserVer ?? "-"} firstErr={attemptLogs[0]}");
                        }
                        succeeded = true;
                        break;
                    }
                    catch (Exception ex)
                    {
                        if (created != null)
                        {
                            try { created.Close(); } catch (COMException) { }
                            created = null;
                        }
                        lastEx = ex;
                        lastStep = step;
                        attemptLogs.Add($"{attempt.Label}[step={step}, ver={browserVer ?? "-"}]: {ex.GetType().Name} 0x{ex.HResult:X8}: {ex.Message}");
                    }
                }
            }
            finally
            {
                Environment.SetEnvironmentVariable("TEMP", origTemp);
                Environment.SetEnvironmentVariable("TMP", origTmp);
            }

            if (!succeeded && lastEx != null && !disposed && version == lifetime)
            {
                controller = null;
                environment = null;
                documentReady = false;
                lastInitError = $"{lastEx.GetType().Name} 0x{lastEx.HResult:X8} (step={lastStep}): {lastEx.Message}";

                var sb = new StringBuilder();
                var rootHwnd = hostWindow != IntPtr.Zero ? GetAncestor(hostWindow, 2) : IntPtr.Zero;
                var parentHwnd = hostWindow != IntPtr.Zero ? GetParent(hostWindow) : IntPtr.Zero;
                sb.AppendLine($"[{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss zzz}] [FAIL] profile={profile} apt={Thread.CurrentThread.GetApartmentState()} host=0x{hostWindow.ToInt64():X} isWin={(hostWindow != IntPtr.Zero && IsWindow(hostWindow))} parent=0x{parentHwnd.ToInt64():X} root=0x{rootHwnd.ToInt64():X} rootVis={(rootHwnd != IntPtr.Zero && IsWindowVisible(rootHwnd))} bounds={Bounds.Width:0.#}x{Bounds.Height:0.#}");
                sb.AppendLine($"  origTemp={origTemp ?? "<null>"} origTmp={origTmp ?? "<null>"} safeTemp={safeTemp}");
                foreach (var item in attemptLogs) sb.AppendLine($"  attempt: {item}");
                if (!string.IsNullOrWhiteSpace(lastEx.StackTrace)) sb.AppendLine($"  stack: {lastEx.StackTrace.Trim()}");

                AppendFilteredChromeDebug(sb, profile + "-sw");
                AppendFilteredChromeDebug(sb, profile + "-sw2");
                AppendDiagLog(diagPath, sb.ToString().TrimEnd());

                var fallback = IsMali ? "可点上方“系统浏览器打开 ↗”，并在下方备选导入框粘贴结果。"
                    : IsToolbox ? "可重试连接，或用系统浏览器打开。" : "可打开作者原站；本地规划仍可使用。";
                StatusChanged?.Invoke($"内嵌浏览器启动失败（{lastEx.GetType().Name}，0x{lastEx.HResult:X8}，步骤 {lastStep}）。{fallback}");
            }
        }
        catch (Exception error)
        {
            if (created != null) { try { created.Close(); } catch (COMException) { } }
            if (!disposed && version == lifetime)
            {
                controller = null;
                environment = null;
                documentReady = false;
                lastInitError = $"{error.GetType().Name} 0x{error.HResult:X8} (step=Outer): {error.Message}";
                AppendDiagLog(diagPath,
                    $"[{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss zzz}] [OUTER-FAIL] profile={profile} err={lastInitError}{Environment.NewLine}{error.StackTrace}");
                var fallback = IsMali ? "可点上方“系统浏览器打开 ↗”，并在下方备选导入框粘贴结果。"
                    : IsToolbox ? "可重试连接，或用系统浏览器打开。" : "可打开作者原站；本地规划仍可使用。";
                StatusChanged?.Invoke($"内嵌浏览器启动失败（{error.GetType().Name}，0x{error.HResult:X8}，步骤 Outer）。{fallback}");
            }
        }
        finally { if (version == lifetime) initializing = false; }
    }

    private static void AppendDiagLog(string diagPath, string text)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(diagPath)!);
            File.AppendAllText(diagPath, text + Environment.NewLine);
        }
        catch { }
    }

    private static void AppendFilteredChromeDebug(StringBuilder sb, string profileDir)
    {
        try
        {
            var chromeLog = Path.Combine(profileDir, "EBWebView", "chrome_debug.log");
            if (!File.Exists(chromeLog)) return;
            var keywords = new[] { "ERROR", "FATAL", "WARNING", "WebView2HostRoot", "embedded_browser", "mojo", "ipc", "sandbox", "gpu", "code_integrity" };
            var matched = new List<string>(40);
            foreach (var line in File.ReadLines(chromeLog, Encoding.UTF8))
            {
                for (var i = 0; i < keywords.Length; i++)
                {
                    if (line.IndexOf(keywords[i], StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        matched.Add(line);
                        break;
                    }
                }
                if (matched.Count >= 40) break;
            }
            if (matched.Count == 0) return;
            sb.AppendLine($"  chrome_debug ({profileDir}, {matched.Count} lines):");
            foreach (var line in matched) sb.AppendLine($"    {line}");
        }
        catch { }
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
            var allowed = IsMali ? IsAllowedMaliUrl(e.Uri, isSubframe: false) : IsAllowedUrl(allowedOrigin, e.Uri);
            if (!allowed)
            {
                e.Cancel = true;
                StatusChanged?.Invoke(IsMali ? "已拦截离开吗哩吗哩/B站登录域的站外跳转。"
                    : IsToolbox ? "已拦截离开本机工具箱的跳转。" : "站外跳转未自动打开，可使用“作者原站”入口。");
                return;
            }
            documentReady = false;
            ++navigationVersion;
            pendingUrl = e.Uri;
        };
        web.FrameNavigationStarting += (_, e) =>
        {
            var allowed = IsMali ? IsAllowedMaliUrl(e.Uri, isSubframe: true) : (e.Uri == "about:blank" || IsAllowedUrl(allowedOrigin, e.Uri));
            if (!allowed) e.Cancel = true;
        };
        web.LaunchingExternalUriScheme += (_, e) => e.Cancel = true;
        web.NewWindowRequested += (_, e) =>
        {
            e.Handled = true;
            if (IsMali && IsAllowedMaliUrl(e.Uri, isSubframe: false))
            {
                documentReady = false;
                ++navigationVersion;
                pendingUrl = e.Uri;
                web.Navigate(e.Uri);
                return;
            }
            StatusChanged?.Invoke(IsMali ? "已拦截站外弹窗；请在本窗口内完成 B 站登录。"
                : IsToolbox ? "工具箱内嵌页未打开新窗口。可在系统浏览器中查看链接。"
                : "网页新窗口未自动打开，可使用“作者原站”入口。");
        };
        web.DownloadStarting += (_, e) =>
        {
            e.Cancel = true;
            StatusChanged?.Invoke(IsToolbox ? "需要下载或导出时，请用系统浏览器打开工具箱。" : "内嵌页面不下载文件；需要导出时请打开作者原站。");
        };
        web.NavigationCompleted += (_, e) =>
        {
            var sourceUrl = controller?.CoreWebView2.Source ?? "";
            var sourceOk = controller != null && (IsMali ? IsAllowedMaliUrl(sourceUrl, isSubframe: false) : IsAllowedUrl(allowedOrigin, sourceUrl));
            documentReady = e.IsSuccess && e.HttpStatusCode < 400 && sourceOk;
            if (documentReady)
            {
                if (IsMali)
                {
                    var onMaliHost = IsAllowedUrl(allowedOrigin, sourceUrl);
                    StatusChanged?.Invoke(lostPageState ? "网页已重新加载，请检查登录状态后再检索。"
                        : onMaliHost ? "吗哩吗哩页面已就绪；如尚未登录请先手动登录 B 站账号，然后点上方按钮一键检索。"
                        : "正在 B 站登录页，完成手动登录后请点『回到吗哩页面』。");
                }
                else
                {
                    StatusChanged?.Invoke(lostPageState ? "网页已重新加载，请检查未保存的输入。"
                        : IsToolbox ? "资料工具箱已在面板内打开。" : "作者原站 · " + allowedOrigin.Host);
                }
                lostPageState = false;
            }
            else if (IsMali && !e.IsSuccess && e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled)
            {
                StatusChanged?.Invoke("正在跳转页面，请稍候…");
            }
            else StatusChanged?.Invoke(FormatNavigationFailure(e.WebErrorStatus.ToString(),e.HttpStatusCode,IsToolbox));
        };
        web.ProcessFailed += (_, _) =>
        {
            documentReady = false;
            ++navigationVersion;
            StatusChanged?.Invoke(IsToolbox ? "工具箱内嵌页异常。请重试连接，或用系统浏览器打开。"
                : "网页进程异常，请重新打开作者原站；未保存的输入可能丢失。");
        };
    }

    internal static string NavigationFailureMessage(string error,int httpStatus) => FormatNavigationFailure(error,httpStatus,false);

    private static string FormatNavigationFailure(string error,int httpStatus,bool toolbox)
    {
        var http=httpStatus>0?$"HTTP {httpStatus}":"未收到站点响应";
        var fallback = toolbox ? "可重试连接，或用系统浏览器打开。" : "可打开作者原站；本地规划仍可使用。";
        return $"网页没有打开（{http}；浏览器错误：{error}）。{fallback}";
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
            StatusChanged?.Invoke(IsToolbox ? "内嵌显示暂不可用，可重试连接或用系统浏览器打开。"
                : "网页显示暂不可用，请重新打开作者原站。");
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
        environment = null;
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

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandleW(string? lpModuleName);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr LoadLibraryW(string lpLibFileName);
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr hModule, string lpProcName);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern ushort RegisterClassW(ref WNDCLASS lpWndClass);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr DefWindowProcW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateWindowEx(int extended, string className, string name, int style,
        int x, int y, int width, int height, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr parameter);
    [DllImport("user32.dll")]
    private static extern bool DestroyWindow(IntPtr hwnd);
    [DllImport("user32.dll")]
    private static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")]
    private static extern IntPtr GetParent(IntPtr hWnd);
    [DllImport("user32.dll")]
    private static extern IntPtr GetAncestor(IntPtr hwnd, uint gaFlags);
    [DllImport("ole32.dll")]
    private static extern int OleInitialize(IntPtr pvReserved);
}
