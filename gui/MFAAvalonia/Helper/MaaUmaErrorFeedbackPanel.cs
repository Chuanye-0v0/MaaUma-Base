using Avalonia.Controls;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace MFAAvalonia.Helper;

/// <summary>User-initiated report for the current exception only; no startup/network hook.</summary>
public sealed class MaaUmaErrorFeedbackPanel : StackPanel
{
    private readonly Func<JObject, Task<JObject>> request;
    private readonly string root;
    private readonly Button submit = new() { Name="MaaUmaSubmitPrivateError", Content="私密提交错误报告" };
    private readonly TextBlock status = new() { Name="MaaUmaPrivateErrorStatus", TextWrapping=Avalonia.Media.TextWrapping.Wrap,
        Text="仅提交版本和脱敏后的当前异常，不含配置或截图；仅维护者可见。" };
    private JObject? current;
    private string? reportId, previewText, feedbackId;
    private bool busy, submitted;
    private bool taskFailure;
    private string? prepareCode;

    public MaaUmaErrorFeedbackPanel() : this(AppContext.BaseDirectory) { }
    public MaaUmaErrorFeedbackPanel(string packageRoot, Func<JObject, Task<JObject>>? service=null)
    {
        root=Path.GetFullPath(packageRoot);
        request=service ?? new ReportClient(root).Request;
        Spacing=6;
        Children.Add(status);
        Children.Add(submit);
        submit.Click+=async (_,_)=>await SubmitCurrentAsync();
        Buttons();
    }

    private void Buttons()=>submit.IsEnabled=current!=null && !busy && !submitted;
    private static string Limited(string? text,int count)=>text==null ? "" : text.Length<=count ? text : text[..count];

    public void SetReport(string? message,string? details,string? exceptionType=null,string? reportContext=null)
    {
        if(busy)return;
        taskFailure=false;
        current=new JObject { ["action"]="prepare", ["app_version"]=AppVersion(),
            ["exception_type"]=Limited(exceptionType,256), ["message"]=Limited(message,8000),
            ["stack_trace"]=Limited(details,24000), ["recent_actions"]=new JArray() };
        if(reportContext=="training")current["report_context"]="training";
        reportId=previewText=feedbackId=null;
        submitted=false;
        submit.Content="私密提交错误报告";
        status.Text="仅提交版本和脱敏后的当前异常，不含配置或截图；仅维护者可见。";
        Buttons();
    }

    public void SetLatestTaskFailure()
    {
        if(busy)return;
        taskFailure=true;
        current=new JObject { ["action"]="prepare_task_failure", ["app_version"]=AppVersion() };
        reportId=previewText=feedbackId=prepareCode=null;
        submitted=false;
        submit.Content="私密提交失败报告";
        status.Text="仅提交最近一次失败的任务、版本和脱敏异常；不含配置或截图，仅维护者可见。";
        Buttons();
    }

    private string AppVersion()
    {
        try {
            var path=Path.Combine(root,"interface.json");
            var value=(string?)JObject.Parse(File.ReadAllText(path,Encoding.UTF8))["version"];
            return value!=null && Regex.IsMatch(value,@"\A[0-9A-Za-z.+_-]{1,64}\z") ? value : "unknown";
        }catch{return "unknown";}
    }

    private async Task<bool> PrepareCurrentAsync()
    {
        if(reportId!=null && previewText!=null)return true;
        if(current==null)return false;
        var value=await request((JObject)current.DeepClone());
        prepareCode=(string?)value["code"];
        if((bool?)value["ok"]!=true || !Guid.TryParseExact((string?)value["report_id"],"D",out var id)
            || value["preview_text"]?.Type!=JTokenType.String)return false;
        var preview=(string?)value["preview_text"];
        if(string.IsNullOrWhiteSpace(preview) || preview.Length>65536)return false;
        reportId=id.ToString("D");previewText=preview;
        return true;
    }

    public async Task SubmitCurrentAsync()
    {
        if(busy || submitted || current==null)return;
        busy=true;Buttons();prepareCode=null;status.Text="正在准备并私密提交本次错误…";
        try {
            if(!await PrepareCurrentAsync())throw new IOException();
            var result=await request(new JObject { ["action"]="submit", ["report_id"]=reportId });
            var feedback=(string?)result["feedback_id"];
            if((bool?)result["ok"]!=true || feedback==null
                || !Regex.IsMatch(feedback,@"\ABF-[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}\z",RegexOptions.IgnoreCase))throw new IOException();
            feedbackId=feedback;
            submitted=true;submit.Content="已私密提交";
            status.Text="提交成功，反馈编号："+feedback+"。仅维护者可见。";
        }catch {
            submit.Content="重试私密提交";
            status.Text=taskFailure && prepareCode=="error_feedback_no_task_failure"
                ? "尚无可提交的任务失败记录。请在任务失败后使用此入口；旧版本的历史失败仍可通过“导出日志”提供。"
                : "提交未完成，可重试或复制脱敏错误报告。";
        }finally{busy=false;Buttons();}
    }

    public async Task CopyCurrentAsync(Func<string,Task> copy)
    {
        if(busy || current==null)return;
        busy=true;Buttons();
        try {
            if(!await PrepareCurrentAsync())throw new IOException();
            await copy(previewText!);
            status.Text="已复制脱敏错误报告。"+(feedbackId!=null ? "反馈编号："+feedbackId : "");
        }catch{status.Text="脱敏报告暂未准备好，可重试复制或私密提交。";}
        finally{busy=false;Buttons();}
    }

    private sealed class ReportClient(string root)
    {
        public async Task<JObject> Request(JObject input)
        {
            var python=Path.Combine(root,"runtime","python.exe");
            var script=Path.Combine(root,"backend","scripts","maauma_error_feedback.py");
            var info=new ProcessStartInfo(python) { WorkingDirectory=root, UseShellExecute=false, CreateNoWindow=true,
                RedirectStandardInput=true, RedirectStandardOutput=true, RedirectStandardError=true,
                StandardInputEncoding=new UTF8Encoding(false), StandardOutputEncoding=Encoding.UTF8,
                StandardErrorEncoding=Encoding.UTF8 };
            foreach(var arg in new[]{"-B","-X","utf8",script,"--root",root})info.ArgumentList.Add(arg);
            info.Environment["DOTNET_ROOT"]=Path.Combine(root,"dotnet");
            using var process=Process.Start(info) ?? throw new IOException();
            var output=process.StandardOutput.ReadToEndAsync();
            var error=process.StandardError.ReadToEndAsync();
            await process.StandardInput.WriteAsync(input.ToString(Formatting.None));
            process.StandardInput.Close();
            using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(60));
            try { await process.WaitForExitAsync(timeout.Token); }
            catch(OperationCanceledException){try{process.Kill(true);}catch{}throw new IOException();}
            var text=await output;_=await error;
            if(text.Length>131072)throw new IOException();
            return JObject.Parse(text);
        }
    }
}
