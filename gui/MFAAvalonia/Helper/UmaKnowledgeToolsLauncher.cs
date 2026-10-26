using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace MFAAvalonia.Helper;

/// <summary>Starts only the local knowledge app, with an independent browser handoff.</summary>
public sealed class UmaKnowledgeToolsLauncher
{
    public const string AppId = "maauma-knowledge-hub";
    public static readonly Uri LocalBrowserUrl = new("http://127.0.0.1:18765/");
    public Uri BrowserUrl { get; }
    public enum HubState { Absent, Matching, Occupied }
    private static readonly SemaphoreSlim LaunchGate = new(1, 1);
    private readonly string applicationRoot;
    private readonly Func<Task<HubState>> probe;
    private readonly Func<ProcessStartInfo, bool> start;
    private readonly Func<Uri, Task> openBrowser;
    private readonly Func<TimeSpan, Task> delay;

    public UmaKnowledgeToolsLauncher(string? applicationRoot = null,
        Func<Task<HubState>>? probe = null, Func<ProcessStartInfo, bool>? start = null,
        Func<Uri, Task>? openBrowser = null, Func<TimeSpan, Task>? delay = null)
    {
        this.applicationRoot = Path.GetFullPath(applicationRoot ?? AppContext.BaseDirectory);
        var port=18765;var sidecar=Path.Combine(this.applicationRoot,"knowledge-tools.launcher.json");
        if(File.Exists(sidecar)) { using var config=JsonDocument.Parse(File.ReadAllText(sidecar));
            if(config.RootElement.TryGetProperty("port",out var configured))port=configured.GetInt32(); }
        if(port is not (18765 or 18766))throw new InvalidOperationException("资料工具箱只使用本机正式或预览端口。");
        BrowserUrl=new Uri($"http://127.0.0.1:{port}/");
        this.probe = probe ?? (()=>ProbeLocalHub(BrowserUrl));
        this.start = start ?? StartProcess;
        this.openBrowser = openBrowser ?? OpenLocalBrowser;
        this.delay = delay ?? Task.Delay;
    }

    public async Task<string> OpenAsync(string? contextId=null)
    {
        await EnsureReadyAsync();
        var url=BrowserUrl;
        if(!string.IsNullOrEmpty(contextId))url=new Uri(BrowserUrl,"#context="+Uri.EscapeDataString(contextId));
        await openBrowser(url);return "资料工具箱已打开；带入与取回均为本地草稿。";
    }

    public async Task EnsureReadyAsync()
    {
        await LaunchGate.WaitAsync();
        try
        {
            var state = await probe();
            if (state == HubState.Matching)
            {
                return;
            }
            if (state == HubState.Occupied)
                throw new InvalidOperationException("本地工具端口已被其他服务占用，未启动工具箱。请关闭占用后重试。");

            var info = CreateStartInfo();
            if (!start(info)) throw new InvalidOperationException("资料工具箱未能启动，请检查本地工具文件。");
            for (var attempt = 0; attempt < 40; attempt++)
            {
                await delay(TimeSpan.FromMilliseconds(250));
                state = await probe();
                if (state == HubState.Matching)
                {
                    return;
                }
                if (state == HubState.Occupied)
                    throw new InvalidOperationException("工具箱启动后未返回正确的服务标识；未打开页面。");
            }
            throw new InvalidOperationException("资料工具箱仍未就绪，请稍后重试。已运行的育成不受影响。");
        }
        finally { LaunchGate.Release(); }
    }

    public ProcessStartInfo CreateStartInfo()
    {
        var python = Path.Combine(applicationRoot, "runtime", "pythonw.exe");
        var script = Path.Combine(applicationRoot, "knowledge-tools", "app.py");
        // Optional local-development sidecar. Never searches PATH or reads account settings.
        var sidecar = Path.Combine(applicationRoot, "knowledge-tools.launcher.json");
        if (File.Exists(sidecar))
        {
            using var config = JsonDocument.Parse(File.ReadAllText(sidecar));
            python = config.RootElement.GetProperty("python_executable").GetString() ?? "";
            script = config.RootElement.GetProperty("app_script").GetString() ?? "";
        }
        if (!Path.IsPathFullyQualified(python) || !Path.IsPathFullyQualified(script)
            || !string.Equals(Path.GetFileName(python), "pythonw.exe", StringComparison.OrdinalIgnoreCase)
            || !string.Equals(Path.GetFileName(script), "app.py", StringComparison.OrdinalIgnoreCase)
            || !File.Exists(python) || !File.Exists(script))
            throw new InvalidOperationException("资料工具箱文件未配齐。请核对 runtime/pythonw.exe 与 knowledge-tools/app.py。");

        var info = new ProcessStartInfo(Path.GetFullPath(python))
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = Path.GetDirectoryName(Path.GetFullPath(script))!,
            WindowStyle = ProcessWindowStyle.Hidden
        };
        info.ArgumentList.Add(Path.GetFullPath(script));
        info.ArgumentList.Add("--port");
        info.ArgumentList.Add(BrowserUrl.Port.ToString(System.Globalization.CultureInfo.InvariantCulture));
        return info;
    }

    public static bool IsMatchingStatus(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            return doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty("app_id", out var appId)
                && appId.ValueKind == JsonValueKind.String && appId.GetString() == AppId
                && doc.RootElement.TryGetProperty("integration_protocol",out var protocol)
                && protocol.ValueKind==JsonValueKind.Number&&protocol.TryGetInt32(out var version)&&version==1;
        }
        catch (JsonException) { return false; }
    }

    public static Task<HubState> ProbeLocalHub()=>ProbeLocalHub(LocalBrowserUrl);
    private static async Task<HubState> ProbeLocalHub(Uri origin)
    {
        using var handler = new HttpClientHandler { UseProxy = false, AllowAutoRedirect = false };
        using var client = new HttpClient(handler) { Timeout = TimeSpan.FromMilliseconds(650) };
        try
        {
            using var response = await client.GetAsync(new Uri(origin, "api/status"), HttpCompletionOption.ResponseHeadersRead);
            if (response.StatusCode != HttpStatusCode.OK || response.Content.Headers.ContentLength > 65536)
                return HubState.Occupied;
            // Bound both the response body and its read time; do not trust a foreign local service.
            using var timeout = new CancellationTokenSource(TimeSpan.FromMilliseconds(650));
            await using var body = await response.Content.ReadAsStreamAsync(timeout.Token);
            var buffer = new byte[65537];
            var length = 0;
            while (length < buffer.Length)
            {
                var read = await body.ReadAsync(buffer.AsMemory(length), timeout.Token);
                if (read == 0) break;
                length += read;
            }
            return length <= 65536 && IsMatchingStatus(System.Text.Encoding.UTF8.GetString(buffer, 0, length))
                ? HubState.Matching : HubState.Occupied;
        }
        catch (HttpRequestException error) when (error.InnerException is SocketException socket
            && socket.SocketErrorCode == SocketError.ConnectionRefused) { return HubState.Absent; }
        catch (HttpRequestException) { return HubState.Occupied; }
        catch (OperationCanceledException) { return HubState.Occupied; }
        catch (IOException) { return HubState.Occupied; }
    }

    private static bool StartProcess(ProcessStartInfo info)
    {
        using var process = Process.Start(info);
        return process != null;
    }
    private static Task OpenLocalBrowser(Uri url)
    {
        if (url.Scheme!="http"||url.Host!="127.0.0.1"||url.Port is not (18765 or 18766)||url.AbsolutePath!="/"||url.Query.Length>0)
            throw new InvalidOperationException("只允许打开本地资料工具箱。");
        using var process = Process.Start(new ProcessStartInfo(url.AbsoluteUri) { UseShellExecute = true });
        if (process == null) throw new InvalidOperationException("浏览器未能打开，请手动访问 http://127.0.0.1:18765/ 。");
        return Task.CompletedTask;
    }
}
