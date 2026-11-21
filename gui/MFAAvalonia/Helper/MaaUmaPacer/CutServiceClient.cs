using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Diagnostics;
using System.Text;
using System.Globalization;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

internal interface ICutServiceClient
{
    Task<JObject> Request(JObject request);
    Task<JObject> Feedback(JObject request,Action<JObject>? progress=null,CancellationToken cancellationToken=default)=>Request(request);
    Task<JObject> Update(string action,Action<JObject>? progress=null,CancellationToken cancellationToken=default);
}

internal sealed class CutServiceClient(string root,string? pythonOverride=null,
    TimeSpan? downloadTimeout=null,TimeSpan? stallTimeout=null):ICutServiceClient
{
    private readonly SemaphoreSlim gate=new(1,1);

    public Task<JObject> Request(JObject request)=>Run("cut_support_client.py",null,request);
    public Task<JObject> Feedback(JObject request,Action<JObject>? progress=null,CancellationToken cancellationToken=default)
    {
        var action=(string?)request["action"];
        if(action is not ("submit_feedback" or "submit_runtime_stop_report"))throw new InvalidOperationException("不支持此上传操作。");
        return Run(action=="submit_feedback"?"cut_support_client.py":"submit_runtime_stop_report.py",null,request,progress,cancellationToken);
    }
    public Task<JObject> Update(string action,Action<JObject>? progress=null,CancellationToken cancellationToken=default)
    {
        if(action is not ("check" or "prepare"))throw new InvalidOperationException("不支持此更新操作。");
        return Run("product_update.py",action,null,progress,cancellationToken);
    }

    private async Task<JObject> Run(string script,string? action,JObject? input,
        Action<JObject>? progress=null,CancellationToken cancellationToken=default)
    {
        await gate.WaitAsync(cancellationToken);
        try {
            var python=pythonOverride ?? Path.Combine(root,"runtime","python.exe");
            var path=Path.Combine(root,"backend","scripts",script);
            if(!File.Exists(python) || !File.Exists(path))throw new IOException("此安装包缺少服务接口，请使用完整安装包。");
            var info=new ProcessStartInfo(python) {
                WorkingDirectory=root,UseShellExecute=false,CreateNoWindow=true,
                RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,
                StandardInputEncoding=new UTF8Encoding(false),StandardOutputEncoding=Encoding.UTF8,
                StandardErrorEncoding=Encoding.UTF8
            };
            foreach(var argument in new[]{"-B","-X","utf8",path,"--root",root})info.ArgumentList.Add(argument);
            if(action!=null)info.ArgumentList.Add(action);
            var downloadId=action=="prepare" ? Guid.NewGuid().ToString("N") : null;
            if(downloadId!=null) {
                info.ArgumentList.Add("--progress");
                info.ArgumentList.Add("--download-id");info.ArgumentList.Add(downloadId);
            }
            var temp=Path.Combine(root,"temp");Directory.CreateDirectory(temp);
            info.Environment["TEMP"]=info.Environment["TMP"]=temp;
            using var process=Process.Start(info) ?? throw new IOException("未能启动服务接口。");
            try {
            var feedback=input?["action"]?.Type==JTokenType.String && (string?)input["action"] is "submit_feedback" or "submit_runtime_stop_report";
            using var deadline=new CancellationTokenSource(feedback?Timeout.InfiniteTimeSpan:action=="prepare" ? downloadTimeout ?? TimeSpan.FromHours(2) : TimeSpan.FromMinutes(2));
            using var stalled=new CancellationTokenSource();
            using var invalidUploadOutput=new CancellationTokenSource();
            using var linked=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken,deadline.Token,stalled.Token,invalidUploadOutput.Token);
            var idle=stallTimeout ?? TimeSpan.FromSeconds(75);
            await using var watch=new System.Threading.Timer(_=>stalled.Cancel(),null,
                action=="prepare" || feedback ? idle : Timeout.InfiniteTimeSpan,Timeout.InfiniteTimeSpan);
            var output=feedback?ReadFeedbackOutput():process.StandardOutput.ReadToEndAsync();
            var errors=feedback?ReadFeedbackProgress():ReadProgress();
            async Task<string> ReadFeedbackOutput() {
                var buffer=new char[2048];var result=new StringBuilder();bool overflow=false;int count;
                while((count=await process.StandardOutput.ReadAsync(buffer.AsMemory()))>0) {
                    if(!overflow && result.Length+count<=65536)result.Append(buffer,0,count);
                    else {overflow=true;result.Clear();invalidUploadOutput.Cancel();}
                }
                return result.ToString();
            }
            async Task ReadFeedbackProgress() {
                var buffer=new char[1024];var line=new StringBuilder();bool overflow=false;int count;
                long previous=-1,totalSeen=-1;
                void Accept() {
                    if(overflow||line.Length==0)return;
                    try {
                        var entry=JObject.Parse(line.ToString());
                        if(entry.Count!=4 || entry.Properties().Any(p=>p.Name is not ("schema_version" or "stage" or "completed_parts" or "total_parts"))
                           || entry["schema_version"]?.Type!=JTokenType.String || (string?)entry["schema_version"]!="maauma.feedback_upload_progress.v1"
                           || entry["stage"]?.Type!=JTokenType.String || (string?)entry["stage"]!="upload"
                           || entry["completed_parts"]?.Type!=JTokenType.Integer || entry["total_parts"]?.Type!=JTokenType.Integer)return;
                        if(!long.TryParse(entry["completed_parts"]!.ToString(),NumberStyles.None,CultureInfo.InvariantCulture,out var parts)
                           || !long.TryParse(entry["total_parts"]!.ToString(),NumberStyles.None,CultureInfo.InvariantCulture,out var total))return;
                        if(total<1||total>9007199254740991L||parts<0||parts>total||totalSeen!=-1&&totalSeen!=total||parts<=previous)return;
                        previous=parts;totalSeen=total;watch.Change(idle,Timeout.InfiniteTimeSpan);
                        progress?.Invoke(new JObject{["schema_version"]="maauma.feedback_upload_progress.v1",["stage"]="upload",["completed_parts"]=parts,["total_parts"]=total});
                    }catch(Newtonsoft.Json.JsonException){}catch(OverflowException){}
                }
                while((count=await process.StandardError.ReadAsync(buffer.AsMemory()))>0) {
                    for(var i=0;i<count;i++) {
                        var c=buffer[i];if(c=='\n'){Accept();line.Clear();overflow=false;}
                        else if(!overflow && c!='\r'){if(line.Length<2048)line.Append(c);else{line.Clear();overflow=true;}}
                    }
                }
                Accept();
            }
            async Task ReadProgress() {
                string? previousStage=null;long previousBytes=0;long previousParts=-1,totalParts=-1;
                while(await process.StandardError.ReadLineAsync() is string line) {
                    if(action!="prepare"&&!feedback)continue;
                    JObject entry;
                    try {entry=JObject.Parse(line);}catch(Newtonsoft.Json.JsonException){continue;}
                    if(feedback){
                        if((string?)entry["schema_version"]!="maauma.feedback_upload_progress.v1" || (string?)entry["stage"]!="upload"
                           || entry["completed_parts"]?.Type!=JTokenType.Integer || entry["total_parts"]?.Type!=JTokenType.Integer)continue;
                        var parts=(long)entry["completed_parts"]!;var total=(long)entry["total_parts"]!;
                        if(total<1 || parts<0 || parts>total || totalParts!=-1&&totalParts!=total || parts<=previousParts)continue;
                        totalParts=total;previousParts=parts;watch.Change(idle,Timeout.InfiniteTimeSpan);progress?.Invoke(entry);continue;
                    }
                    if((string?)entry["event"]!="update_progress")continue;
                    var stage=(string?)entry["stage"];
                    var bytes=entry["downloaded_bytes"]?.Type==JTokenType.Integer ? (long)entry["downloaded_bytes"]! : 0;
                    // A few keepalive bytes must not hide a stalled transfer.
                    if(stage!=previousStage || bytes-previousBytes>=16*1024) {
                        watch.Change(idle,Timeout.InfiniteTimeSpan);previousStage=stage;previousBytes=bytes;
                    }
                    progress?.Invoke(entry);
                }
            }
            // Invitations and descriptions are stdin-only, never arguments or logs.
            try {
                if(input!=null)await process.StandardInput.WriteAsync(input.ToString(Newtonsoft.Json.Formatting.None).AsMemory(),linked.Token);
                process.StandardInput.Close();
                await process.WaitForExitAsync(linked.Token);
                cancellationToken.ThrowIfCancellationRequested();
            } catch(OperationCanceledException) {
                // This process only checks/downloads service data; it cannot
                // start an installer or issue game input. Never kill the host.
                try {if(!process.HasExited)process.Kill(entireProcessTree:true);}
                catch(InvalidOperationException) when(process.HasExited) { }
                await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
                _=await output;await errors;
                cancellationToken.ThrowIfCancellationRequested();
                var code=feedback?invalidUploadOutput.IsCancellationRequested?"feedback_upload_invalid_response":"feedback_upload_stalled":action=="prepare" ? stalled.IsCancellationRequested ? "download_stalled" : "download_timeout" : "service_timeout";
                return new JObject {["ok"]=false,["status"]="error",["code"]=code,["message"]=
                    code=="feedback_upload_invalid_response" ? "上传接口返回了无效结果，原报告与分段进度保留，可重试。" :
                    code=="feedback_upload_stalled" ? "上传超过75秒没有有效进展，原稿和分段进度保留，可重试同一份报告。" :
                    code=="download_stalled" ? "下载超过75秒没有有效进展，已停止等待；当前程序未替换，可以重试。" :
                    code=="download_timeout" ? "下载等待超过两小时，已停止等待；当前程序未替换。" : "服务操作等待超时，可以稍后重试。"};
            }
            var text=await output;await errors;
            try {
                var result=JObject.Parse(text);
                if(result["ok"]?.Type!=JTokenType.Boolean)throw new IOException();
                return result;
            } catch(Exception error) when(error is Newtonsoft.Json.JsonException or IOException) {
                throw new IOException("服务接口没有返回有效结果；没有确认成功。",null);
            }
            } finally {
                // Only this request's identified download is eligible. The
                // finalizer also verifies ownership and skips prepared data.
                if(downloadId!=null && process.HasExited)
                    await FinishDownload(python,path,temp,downloadId,process.Id);
            }
        } catch(Exception error) when(error is System.ComponentModel.Win32Exception) {
            throw new IOException("未能启动服务接口，请检查完整安装包。",null);
        } finally {gate.Release();}
    }

    private async Task FinishDownload(string python,string script,string temp,string downloadId,int ownerPid)
    {
        Process? cleanup=null;
        try {
            var info=new ProcessStartInfo(python) {WorkingDirectory=root,UseShellExecute=false,CreateNoWindow=true,
                RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8};
            foreach(var argument in new[]{"-B","-X","utf8",script,"--root",root,"cleanup-download",
                "--download-id",downloadId,"--download-pid",ownerPid.ToString()})info.ArgumentList.Add(argument);
            info.Environment["TEMP"]=info.Environment["TMP"]=temp;
            cleanup=Process.Start(info);if(cleanup==null)return;
            var output=cleanup.StandardOutput.ReadToEndAsync();var errors=cleanup.StandardError.ReadToEndAsync();
            using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(8));
            await cleanup.WaitForExitAsync(timeout.Token);await output;await errors;
        } catch(Exception) {
            // Cache cleanup must not turn a completed download into a failure.
            try {if(cleanup is {HasExited:false})cleanup.Kill(entireProcessTree:true);}catch(Exception) { }
        } finally {cleanup?.Dispose();}
    }
}

internal sealed class CutUpdateInstaller(string root,Func<Task<JObject>> snapshot,
    Func<Task> closeBackend,Action<bool> exclusive,Action<ProcessStartInfo> launch,Action closeWindow)
{
    public async Task Install(JObject prepared)
    {
        var current=await snapshot();
        if((current["control"] as JObject)?["state"]?.Value<string>() is not ("idle" or "stopped"))
            throw new InvalidOperationException("请先停止育成，等待后台结束后再安装更新。");
        var executable=OwnedPath(prepared,"helper_executable");
        var script=OwnedPath(prepared,"helper_script");
        var ticket=OwnedPath(prepared,"ticket");
        exclusive(true);
        var launched=false;
        try {
            // Closing includes the backend's own stop/snapshot/exit acknowledgement.
            await closeBackend();
            var info=new ProcessStartInfo(executable) {WorkingDirectory=root,UseShellExecute=false,CreateNoWindow=true};
            foreach(var argument in new[]{"-B","-X","utf8",script,"--root",root,"apply","--ticket",ticket,
                "--wait-pid",Environment.ProcessId.ToString(),"--restart"})info.ArgumentList.Add(argument);
            launch(info);launched=true;
            closeWindow();
        } finally {if(!launched)exclusive(false);}
    }

    private string OwnedPath(JObject result,string key)
    {
        var text=(string?)result[key];
        if(string.IsNullOrWhiteSpace(text))throw new IOException("更新助手资料不完整，尚未安装。");
        var path=Path.GetFullPath(text);
        var updates=Path.GetFullPath(Path.Combine(root,"updates"))+Path.DirectorySeparatorChar;
        if(!path.StartsWith(updates,StringComparison.OrdinalIgnoreCase) || !File.Exists(path))
            throw new IOException("更新助手不属于此安装目录，尚未安装。");
        return path;
    }
}
