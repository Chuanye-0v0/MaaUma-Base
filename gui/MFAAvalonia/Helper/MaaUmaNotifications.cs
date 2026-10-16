using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed class MaaUmaNotificationClient(string packageRoot)
{
    private readonly string root=Path.GetFullPath(packageRoot);
    private readonly SemaphoreSlim gate=new(1,1);
    public async Task<JObject> Request(JObject input)
    {
        await gate.WaitAsync();
        try {
            var backend=Path.Combine(root,"backend");var python=Path.Combine(root,"runtime","python.exe");
            if(!File.Exists(python)||!File.Exists(Path.Combine(backend,"desktop","notifications_cli.py")))
                throw new IOException("公共通知组件未安装完整。");
            var info=new ProcessStartInfo(python) {WorkingDirectory=backend,UseShellExecute=false,CreateNoWindow=true,
                RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,
                StandardInputEncoding=new UTF8Encoding(false),StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8};
            foreach(var arg in new[]{"-B","-X","utf8","-m","desktop.notifications_cli","--root",root})info.ArgumentList.Add(arg);
            using var child=Process.Start(info)??throw new IOException("公共通知服务未能启动。");
            var output=child.StandardOutput.ReadToEndAsync();var errors=child.StandardError.ReadToEndAsync();
            await child.StandardInput.WriteAsync(input.ToString(Formatting.None));child.StandardInput.Close();
            input.Remove("token");
            using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(45));
            try {await child.WaitForExitAsync(timeout.Token);}
            catch(OperationCanceledException){try{child.Kill(true);}catch{}throw new IOException("通知响应尚未确认，不会自动重试。");}
            var text=await output;_=await errors;
            if(text.Length>128*1024)throw new IOException("通知响应未通过检查。");
            return JObject.Parse(text);
        }finally {input.Remove("token");gate.Release();}
    }
}

public static class MaaUmaNotifications
{
    private static readonly MaaUmaNotificationClient Client=new(AppContext.BaseDirectory);
    public static Task<JObject> Request(JObject input)=>Client.Request(input);

    // Called once by the existing queue completion guard. Never changes the
    // business result, depends on legacy provider switches, or retries I/O.
    public static async Task SendQueueResultAsync(bool success,string queueId,
        Func<JObject,Task<JObject>>? request=null)
    {
        var eventId=success?"task_completed":"task_failed";
        try {await (request??Request)(new JObject {["action"]="send",["event_id"]=eventId,
            ["title"]=success?"MaaUma 任务完成":"MaaUma 任务失败",
            ["content"]=success?"当前任务队列已完成，请查看面板结果。":"当前任务队列失败，请查看面板记录。",
            ["dedupe_key"]="queue:"+queueId+":"+eventId});}
        catch { /* Notification failure must not fail or replay the task. */ }
    }
}
