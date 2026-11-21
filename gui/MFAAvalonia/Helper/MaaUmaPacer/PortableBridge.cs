using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public interface IUmaPacerBridge
{
    Task<JObject> Request(JObject request);
    string? CloseError => null;
}

public sealed record UmaPacerContext(string Endpoint, bool DailyRunning, bool Connected);

/// <summary>One private stdio host. Never retries an uncertain control request.</summary>
public sealed class UmaPacerBridge : IUmaPacerBridge
{
    private static readonly List<UmaPacerBridge> Bridges = new();
    public static readonly UmaPacerBridge Instance = new();
    private static bool closingAll;
    private readonly SemaphoreSlim gate = new(1, 1);
    private Process? process;
    private bool broken;
    private bool closing;
    private bool closed;
    private bool detachedForExit;
    private readonly object protocolSync = new();
    private Task<string?>? pendingReply;
    private Task? closeTask;
    private Task? stdoutDrain;
    private string? protocolFailure;
    private string? diagnosticRoot;
    public string? CloseError { get; private set; }

    private readonly string rootDirectory;
    private readonly Func<ProcessStartInfo>? launchOverride;
    private readonly bool allowCareer;
    public string DefaultMode {get;private set;}="cut";
    public event Action<JObject>? SnapshotReceived;
    public UmaPacerBridge(string? rootDirectory=null, Func<ProcessStartInfo>? launchOverride=null,bool allowCareer=false)
    {
        this.rootDirectory=Path.GetFullPath(rootDirectory ?? AppContext.BaseDirectory);
        this.launchOverride=launchOverride;
        this.allowCareer=allowCareer;
        lock (Bridges) Bridges.Add(this);
    }

    public static UmaPacerContext ReadContext() => new(PortableConnection.Load(AppContext.BaseDirectory).Endpoint,false,false);

    public async Task PrepareConnectionChange()
    {
        if(process is not {HasExited:false})return;
        var result=await Request(new JObject {["action"]="snapshot"});
        if((string?)result["control"]?["state"] is not ("idle" or "stopped"))
            throw new InvalidOperationException("请先停止当前批次，再修改模拟器连接。");
        await gate.WaitAsync();
        try { process!.StandardInput.Close();await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));process.Dispose();process=null; }
        finally { gate.Release(); }
    }

    private void EnsureProcess()
    {
        if (process is { HasExited: false })
        {
            if (broken) throw new InvalidOperationException("控制通信中断，原后台正在安全停止；不会自动重放指令。 " + protocolFailure);
            return;
        }
        process?.Dispose();
        process = null;
        if (closing || closed) throw new InvalidOperationException("面板正在关闭。");
        var root = rootDirectory;
        var python = Path.Combine(root,"runtime","python.exe");
        var host = Path.Combine(root,"backend","scripts","larc_cut_standalone_host.py");
        if(launchOverride==null && (!File.Exists(python) || !File.Exists(host)))
            throw new InvalidOperationException("便携包不完整：缺少runtime/python.exe或刷切后台，请完整解压。");
        var info = launchOverride?.Invoke() ?? new ProcessStartInfo(python);
        info.WorkingDirectory = root; info.UseShellExecute=false;info.CreateNoWindow=true;
        info.RedirectStandardInput=true;info.RedirectStandardOutput=true;info.RedirectStandardError=true;
        info.StandardInputEncoding=new UTF8Encoding(false);info.StandardOutputEncoding=Encoding.UTF8;info.StandardErrorEncoding=Encoding.UTF8;
        if(launchOverride==null)foreach(var argument in new[] {"-B","-X","utf8","-u",host,"--config",Path.Combine(root,"portable_config.json")})info.ArgumentList.Add(argument);
        info.Environment["PYTHONUTF8"]="1";info.Environment["PYTHONIOENCODING"]="utf-8";info.Environment["PYTHONDONTWRITEBYTECODE"]="1";
        var temporary=Path.Combine(root,"runs","pacer-tmp");Directory.CreateDirectory(temporary);
        info.Environment["TEMP"]=temporary;info.Environment["TMP"]=temporary;
        process = Process.Start(info) ?? throw new InvalidOperationException("育成后台未能启动。");
        diagnosticRoot = root;
        // Drain diagnostics without storing credentials/screens or unbounded text.
        process.ErrorDataReceived += (_, _) => { };
        process.BeginErrorReadLine();
        broken = false;
        stdoutDrain = null;
        protocolFailure = null;
    }

    private void BreakProtocol(Task<string?>? pendingRead = null,string kind = "unspecified")
    {
        lock(protocolSync)
        {
            broken = true;
            var failed = process;
            if(failed == null)return;
            // Closing a pipe may itself wait on a blocked writer. Keep it off
            // the UI thread and preserve the one outstanding stdout reader.
            var read = pendingRead ?? pendingReply;
            var pid=failed.Id;
            protocolFailure=$"[{kind}; pid={pid}]";
            _ = Task.Run(() => {
                var stdinClosed=false;
                // StreamWriter.Close can reject disposal during WriteAsync.
                // Close the owned pipe itself to cancel that pending write.
                try { failed.StandardInput.BaseStream.Close();stdinClosed=true; }
                catch(IOException) { }
                catch(ObjectDisposedException) { }
                catch(InvalidOperationException) { }
                RecordProtocolFailure(pid,kind,stdinClosed);
            });
            stdoutDrain ??= Task.Run(()=>DrainFailedOutput(failed,read));
        }
    }

    private void RecordProtocolFailure(int pid,string kind,bool stdinClosed)
    {
        protocolFailure=$"[{kind}; pid={pid}]";
        // Record categories only: never a raw response, command or credential.
        if(diagnosticRoot == null)return;
        try
        {
            var directory=Path.Combine(diagnosticRoot,"runs","pacer-protocol");Directory.CreateDirectory(directory);
            var stamp=DateTime.UtcNow;
            var data=new JObject { ["type"]=kind,["child_pid"]=pid,["observed_at_utc"]=stamp.ToString("O"),
                ["stdin_close_requested"]=true,["stdin_closed"]=stdinClosed,["stdout_drain_requested"]=true };
            File.WriteAllText(Path.Combine(directory,$"{pid}_{stamp:yyyyMMddTHHmmssfff}.json"),data.ToString(Formatting.None),new UTF8Encoding(false));
        }
        catch(IOException) { }
        catch(UnauthorizedAccessException) { }
    }

    private static async Task DrainFailedOutput(Process failed,Task<string?>? pendingRead)
    {
        try
        {
            // WaitAsync timeout leaves ReadLineAsync alive. Hand that read to
            // the drain instead of starting concurrent reads on StreamReader.
            if(pendingRead != null)await pendingRead.ConfigureAwait(false);
            var buffer = new char[4096];
            while(await failed.StandardOutput.ReadAsync(buffer,0,buffer.Length).ConfigureAwait(false)>0) { }
        }
        catch(IOException) { }
        catch(ObjectDisposedException) { }
    }

    public async Task<JObject> Request(JObject request)
    {
        await gate.WaitAsync();
        try
        {
            EnsureProcess();
            request = (JObject)request.DeepClone();
            request["mode"]=allowCareer ? request["mode"]??DefaultMode : "cut";
            if((string?)request["mode"] is not ("cut" or "career"))throw new InvalidOperationException("未知育成模式，未发送请求。");
            request["request_id"] ??= Guid.NewGuid().ToString("N");
            await process!.StandardInput.WriteLineAsync(request.ToString(Formatting.None));
            await process.StandardInput.FlushAsync();
            string? line;
            Task<string?> pendingRead;
            lock(protocolSync)
            {
                if(broken)throw new InvalidOperationException("原后台正在停止，未发送新的控制请求。");
                pendingRead = pendingReply = process.StandardOutput.ReadLineAsync();
            }
            // A read-only ZIP export must not trigger a control timeout stop.
            try { line = (string?)request["action"]=="export_stop_diagnostics"
                ? await pendingRead : await pendingRead.WaitAsync(TimeSpan.FromSeconds(30)); }
            catch (TimeoutException)
            {
                if((string?)request["action"]!="export_stop_diagnostics")BreakProtocol(pendingRead,"timeout");
                throw new InvalidOperationException("后台响应超时，已请求安全停止；不会重复开始或购买。 " + protocolFailure);
            }
            if (line == null)
            {
                if((string?)request["action"]!="export_stop_diagnostics")BreakProtocol(kind:"eof");
                throw new InvalidOperationException("育成后台已退出，请先查看原记录中的停点。 " + protocolFailure);
            }
            JObject response;
            try { response = JObject.Parse(line); }
            catch(JsonReaderException)
            {
                if((string?)request["action"]!="export_stop_diagnostics")BreakProtocol(kind:"non_json");
                throw new InvalidOperationException("后台输出不是控制回执，已请求安全停止；不会重放指令。 " + protocolFailure);
            }
            if ((string?)response["request_id"] != (string?)request["request_id"])
            {
                if((string?)request["action"]!="export_stop_diagnostics")BreakProtocol(kind:"id_mismatch");
                throw new InvalidOperationException("控制回执不匹配，已请求安全停止。 " + protocolFailure);
            }
            if ((bool?)response["ok"] != true)
                throw new InvalidOperationException((string?)response["error"] ?? "控制请求被拒绝。");
            var result=(JObject)response["result"]!;
            if(allowCareer && (string?)request["action"]!="export_stop_diagnostics") {
                var actual=(string?)result["mode"]??(string?)result["snapshot"]?["mode"];
                DefaultMode=actual is "cut" or "career" ? actual : (string?)request["mode"]??DefaultMode;
            }
            if(!detachedForExit && (string?)request["action"]!="export_stop_diagnostics")SnapshotReceived?.Invoke(result);
            return result;
        }
        finally { lock(protocolSync)pendingReply=null; gate.Release(); }
    }

    public static bool DelayClose(Action retryClose, Func<string,Task<bool>>? confirmFrontendExit=null)
    {
        UmaPacerBridge[] active;
        lock (Bridges) active = Bridges.Where(bridge => !bridge.closed && !bridge.detachedForExit && bridge.process is { HasExited: false }).ToArray();
        if (active.Length == 0) return false;
        if (!closingAll) _ = CloseAllThenRetry(active, retryClose,confirmFrontendExit);
        return true;
    }

    private static async Task CloseAllThenRetry(UmaPacerBridge[] active, Action retryClose,Func<string,Task<bool>>? confirmFrontendExit)
    {
        closingAll = true;
        try
        {
            await Task.WhenAll(active.Select(bridge => bridge.CloseAsync()));
            Dispatcher.UIThread.Post(retryClose);
        }
        catch(Exception error)
        {
            if(confirmFrontendExit!=null && await confirmFrontendExit(error.Message))
            {
                foreach(var bridge in active)
                {
                    bridge.detachedForExit=true;
                    bridge.closing=true;
                    bridge.BreakProtocol(kind:"frontend_exit_pending_stop");
                }
                Dispatcher.UIThread.Post(retryClose);
            }
        }
        finally { closingAll = false; }
    }

    public async Task CloseAsync()
    {
        if (closed || process == null || process.HasExited) return;
        closing = true;
        CloseError = null;
        try
        {
            closeTask ??= CloseCoreAsync();
            // Includes waiting for the request gate, the close RPC and every
            // snapshot. A blocked backend cannot hold the window indefinitely.
            await closeTask.WaitAsync(TimeSpan.FromSeconds(8));
        }
        catch(Exception error)
        {
            CloseError="后台尚未确认停止，当前局仍保留。";
            BreakProtocol(kind:error is TimeoutException ? "close_timeout" : "close_failed");
            if(closeTask!=null)_ = ObserveCloseCompletion(closeTask);
            throw new InvalidOperationException(CloseError,error);
        }
    }

    private static async Task ObserveCloseCompletion(Task task)
    {
        try { await task.ConfigureAwait(false); } catch { /* Reported by CloseError. */ }
    }

    private async Task CloseCoreAsync()
    {
            // EnsureProcess permits requests to the existing process during close.
            await Request(new JObject { ["action"] = "close" });
            var deadline = DateTime.UtcNow.AddSeconds(30);
            while (true)
            {
                var result = await Request(new JObject { ["action"] = "snapshot" });
                if ((string?)result["control"]?["state"] is "idle" or "stopped") break;
                if (DateTime.UtcNow >= deadline)
                    throw new InvalidOperationException("正在等待育成后台收尾，未强制退出；稍后可再次关闭。");
                await Task.Delay(200);
            }
            process!.StandardInput.Close();
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
            closed = true;
    }
}
