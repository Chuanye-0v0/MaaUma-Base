using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public static class MaaUmaUpdateProgressPipe
{
    public const string Schema="maauma.update_progress.v1";
    private static readonly HashSet<string> Stages=new(){"checking","connecting","downloading","verifying","preparing","prepared","current","failed"};
    private static readonly HashSet<string> Steps=new(){"archive_integrity","file_integrity","personalized_integrity","archive_metadata","receipt"};
    private static readonly HashSet<string> TransferKinds=new(){"file_patch","full"};
    private static readonly HashSet<string> FallbackReasons=new(){"no_patch","local_state_mismatch","patch_unusable"};
    public static bool Normalize(JObject value,out JObject safe)
    {
        safe=new JObject();
        try {
            if((string?)value["schema_version"]!=Schema||value["op_id"]?.Type!=JTokenType.String
                ||!Regex.IsMatch((string)value["op_id"]!,"\\A[0-9a-fA-F]{32}\\z")
                ||value["sequence"]?.Type!=JTokenType.Integer||(long)value["sequence"]!<1
                ||value["stage"]?.Type!=JTokenType.String||!Stages.Contains((string)value["stage"]!))return false;
            var item=(string?)value["item_id"];
            if(item is not (null or "base" or "authorized_extension"))return false;
            safe["schema_version"]=Schema;safe["op_id"]=value["op_id"];safe["sequence"]=value["sequence"];
            safe["stage"]=value["stage"];safe["item_id"]=item;
            foreach(var name in new[]{"item_index","item_count","completed_items","failed_items","received_bytes","overall_received_bytes",
                "bytes_per_second","idle_seconds","elapsed_seconds","verification_bytes"}) {
                var token=value[name];if(token==null||token.Type==JTokenType.Null){safe[name]=0;continue;}
                if(token.Type is not (JTokenType.Integer or JTokenType.Float))return false;
                var number=token.Value<double>();if(!double.IsFinite(number)||number<0)return false;safe[name]=number;
            }
            foreach(var name in new[]{"total_bytes","overall_total_bytes","eta_seconds","verification_total_bytes"}) {
                var token=value[name];if(token==null||token.Type==JTokenType.Null){safe[name]=null;continue;}
                if(token.Type is not (JTokenType.Integer or JTokenType.Float))return false;
                var number=token.Value<double>();if(!double.IsFinite(number)||number<0)return false;safe[name]=number;
            }
            safe["waiting"]=(bool?)value["waiting"]==true;
            var status=(string?)value["item_status"];safe["item_status"]=status is "active" or "verified" or "failed"?status:null;
            var step=(string?)value["verification_step"];safe["verification_step"]=step!=null&&Steps.Contains(step)?step:null;
            // Optional 0.4.12 display data. Ignore unknown values without
            // discarding otherwise valid 0.4.11 byte/stage progress.
            if(value["transfer_kind"]?.Type==JTokenType.String
                &&TransferKinds.Contains((string)value["transfer_kind"]!))safe["transfer_kind"]=value["transfer_kind"];
            if(value.Property("fallback_reason") is {} fallback) {
                if(fallback.Value.Type==JTokenType.Null)safe["fallback_reason"]=null;
                else if(fallback.Value.Type==JTokenType.String&&FallbackReasons.Contains((string)fallback.Value))
                    safe["fallback_reason"]=fallback.Value;
            }
            if(DateTimeOffset.TryParse((string?)value["last_received_at"],CultureInfo.InvariantCulture,DateTimeStyles.RoundtripKind,out var last))
                safe["last_received_at"]=last.ToString("O");
            MaaUmaUpdateConnectivity.CopyProgress(value,safe);
            return true;
        }catch {safe=new JObject();return false;}
    }
    public static async Task<int> DrainAsync(TextReader reader,Action<JObject>? receive,CancellationToken cancellationToken=default)
    {
        var count=0;
        try {
            while(await reader.ReadLineAsync(cancellationToken) is string line) {
                if(receive==null||line.Length>65536)continue;
                try {if(JObject.Parse(line) is JObject value&&Normalize(value,out var safe)){
                    try{receive(safe);count++;}catch{/* Display callbacks cannot fail the download. */}}}
                catch{/* Non-protocol diagnostics are never displayed. */}
            }
        }catch(IOException){}catch(OperationCanceledException){}
        return count;
    }
    public static async Task<JObject> ReadResponseAsync(TextReader output,TextReader diagnostics,
        Action<JObject>? receive,CancellationToken cancellationToken=default)
    {
        var stdout=output.ReadToEndAsync(cancellationToken);
        var stderr=DrainAsync(diagnostics,receive,cancellationToken);
        var text=await stdout;_=await stderr;
        if(text.Length>4*1024*1024)throw new IOException();
        return JObject.Parse(text);
    }
}

public sealed class MaaUmaUpdateProgressView : Border
{
    private sealed class Item
    {
        public string Id="",Stage="connecting",Status="active";
        public double Received,Speed,Idle,VerificationBytes;
        public double? Total,Eta,VerificationTotal;
        public string? VerificationStep;
        public string? TransferKind,FallbackReason;
        public DateTimeOffset? LastReceived;
        public bool Waiting;
        public readonly TextBlock Phase=new(){TextWrapping=TextWrapping.Wrap};
        public readonly TextBlock Bytes=new(){TextWrapping=TextWrapping.Wrap};
        public readonly TextBlock Rate=new(){TextWrapping=TextWrapping.Wrap};
        public readonly TextBlock Last=new(){TextWrapping=TextWrapping.Wrap};
        public readonly TextBlock VerifyText=new(){TextWrapping=TextWrapping.Wrap};
        public readonly TextBlock Transfer=new(){TextWrapping=TextWrapping.Wrap};
        public readonly TextBlock Fallback=new(){TextWrapping=TextWrapping.Wrap};
        public readonly ProgressBar Download=new(){Height=8,Minimum=0,Maximum=100};
        public readonly ProgressBar Verify=new(){Height=6,Minimum=0,Maximum=100};
        public readonly StackPanel View=new(){Spacing=7};
        public readonly StackPanel Verification=new(){Spacing=5};
    }
    private readonly TextBlock stage=new(){Name="MaaUmaUpdateStage",TextWrapping=TextWrapping.Wrap,FontSize=17};
    private readonly TextBlock overallBytes=new(){Name="MaaUmaOverallBytes",TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock counts=new(){Name="MaaUmaDownloadCounts",TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock lastByte=new(){Name="MaaUmaOverallLastByte",TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock wait=new(){Name="MaaUmaNetworkWait",TextWrapping=TextWrapping.Wrap};
    private readonly ProgressBar overall=new(){Name="MaaUmaDownloadOverall",Height=9,Minimum=0,Maximum=100};
    private readonly StackPanel rows=new(){Name="MaaUmaDownloadItems",Spacing=12};
    private readonly Dictionary<string,Item> items=new();
    private Guid invocation;
    private string? operation;
    private long sequence;
    private string phase="checking";
    private JObject? connectivity;
    private double overallReceived;
    private double? overallTotal;
    private double globalIdle;
    private bool globalWaiting;
    private DateTimeOffset? globalLastByte;
    private int itemCount,completed,failed;
    private DateTimeOffset eventAt;
    private readonly Func<DateTimeOffset> now;
    public bool IsActive {get;private set;}
    public bool HasFailed {get;private set;}

    public MaaUmaUpdateProgressView():this(()=>DateTimeOffset.UtcNow){}
    public MaaUmaUpdateProgressView(Func<DateTimeOffset> clock)
    {
        now=clock;Name="MaaUmaUpdateProgressCard";IsVisible=false;
        Padding=new Thickness(16);CornerRadius=new CornerRadius(10);BorderThickness=new Thickness(1);
        BorderBrush=Brushes.LightGray;
        Child=new StackPanel {Spacing=9,Children={stage,overall,overallBytes,counts,lastByte,wait,rows}};
    }
    public void Begin(Guid localInvocation)
    {
        invocation=localInvocation;operation=null;sequence=0;phase="checking";connectivity=null;
        items.Clear();rows.Children.Clear();overallReceived=0;overallTotal=null;itemCount=completed=failed=0;
        globalIdle=0;globalWaiting=false;globalLastByte=null;
        IsActive=true;HasFailed=false;IsVisible=true;eventAt=now();
        overall.IsVisible=overallBytes.IsVisible=counts.IsVisible=true;
        stage.Text="正在检查更新与准备下载";overall.IsIndeterminate=true;overall.Value=0;
        overallBytes.Text="等待服务返回实际下载项目和大小。";counts.Text="";wait.Text="";lastByte.Text="正在查询版本，尚未开始接收更新文件。";
    }
    public void Accept(Guid localInvocation,JObject value)
    {
        if(!IsActive||invocation!=localInvocation||!MaaUmaUpdateProgressPipe.Normalize(value,out var data))return;
        var op=(string)data["op_id"]!;var next=(long)data["sequence"]!;
        if(operation!=null&&operation!=op||next<=sequence)return;
        operation=op;sequence=next;eventAt=now();phase=(string)data["stage"]!;
        if(data.Property("network_phase")!=null)connectivity=data;
        overallReceived=(double)data["overall_received_bytes"]!;overallTotal=(double?)data["overall_total_bytes"];
        itemCount=(int)(double)data["item_count"]!;completed=(int)(double)data["completed_items"]!;failed=(int)(double)data["failed_items"]!;
        globalIdle=(double)data["idle_seconds"]!;globalWaiting=(bool)data["waiting"]!;
        globalLastByte=DateTimeOffset.TryParse((string?)data["last_received_at"],out var receivedAt)?receivedAt:null;
        HasFailed=failed>0||phase=="failed";
        if((string?)data["item_id"] is string id) {
            if(!items.TryGetValue(id,out var item)){item=CreateItem(id);items[id]=item;rows.Children.Add(item.View);}
            item.Stage=phase;item.Status=(string?)data["item_status"]??"active";
            if(data.Property("transfer_kind")!=null)item.TransferKind=(string?)data["transfer_kind"];
            if(data.Property("fallback_reason")!=null)item.FallbackReason=(string?)data["fallback_reason"];
            item.Received=(double)data["received_bytes"]!;item.Total=(double?)data["total_bytes"];
            item.Speed=(double)data["bytes_per_second"]!;item.Eta=(double?)data["eta_seconds"];
            item.Idle=(double)data["idle_seconds"]!;item.Waiting=(bool)data["waiting"]!;
            item.VerificationStep=(string?)data["verification_step"];
            item.VerificationBytes=(double)data["verification_bytes"]!;item.VerificationTotal=(double?)data["verification_total_bytes"];
            item.LastReceived=DateTimeOffset.TryParse((string?)data["last_received_at"],out var last)?last:null;
        }
        Render();
    }
    private static Item CreateItem(string id)
    {
        var item=new Item {Id=id};
        var label=id=="base"?"基础程序":"已授权扩展";
        item.Phase.Name="MaaUmaItemPhase_"+id;item.Bytes.Name="MaaUmaItemBytes_"+id;
        item.Rate.Name="MaaUmaItemRate_"+id;item.Download.Name="MaaUmaItemDownload_"+id;
        item.Verify.Name="MaaUmaItemVerification_"+id;
        item.Transfer.Name="MaaUmaItemTransfer_"+id;item.Fallback.Name="MaaUmaItemFallback_"+id;
        item.View.Name="MaaUmaDownloadItem_"+id;
        item.View.Children.Add(new TextBlock {Text=label,FontWeight=FontWeight.SemiBold,FontSize=15});
        foreach(var child in new Control[]{item.Transfer,item.Fallback,item.Phase,item.Download,item.Bytes,item.Rate,item.Last})item.View.Children.Add(child);
        item.Verification.Children.Add(item.VerifyText);item.Verification.Children.Add(item.Verify);
        item.View.Children.Add(item.Verification);return item;
    }
    public void RefreshClock(){if(IsActive)Render();}
    private void Render()
    {
        stage.Text=StageText(phase);
        var connectionText=MaaUmaUpdateConnectivity.ProgressText(connectivity);
        if(phase=="checking"&&connectionText.Length>0)stage.Text=connectionText;
        overall.IsIndeterminate=overallTotal is not >0||phase is "checking" or "verifying" or "preparing";
        overall.Value=Percent(overallReceived,overallTotal);
        overallBytes.Text="总接收："+SizeProgress(overallReceived,overallTotal);
        counts.Text=itemCount>0?$"本次 {itemCount} 项 · 已校验 {completed} 项"+(failed>0?$" · 未准备 {failed} 项":""):"尚未确定需要下载的项目";
        var active=items.Values.LastOrDefault(row=>row.Stage==phase&&row.Status=="active");
        var extra=Math.Max(0,(now()-eventAt).TotalSeconds);
        var waiting=IsActive&&(phase is "connecting" or "downloading")&&(globalWaiting||globalIdle+extra>=8);
        wait.Text=waiting?$"正在等待网络数据 · 已有 {Math.Floor(globalIdle+extra):0} 秒没有新增字节；仍在等待响应。":"";
        lastByte.Text=globalLastByte is {} receivedAt?"最后收到数据："+receivedAt.ToLocalTime().ToString("HH:mm:ss"):phase=="checking"?"版本查询不计入文件流量；尚未开始接收更新文件。":"尚未接收更新文件字节";
        foreach(var item in items.Values) {
            var done=item.Status=="verified";var bad=item.Status=="failed";
            item.Phase.Text=bad?"本项未准备完成":done?"校验完成":StageText(item.Stage);
            item.Transfer.IsVisible=item.TransferKind!=null;
            item.Transfer.Text=item.TransferKind switch {"file_patch"=>"传输方式：增量更新","full"=>"传输方式：完整更新",_=>""};
            item.Fallback.IsVisible=item.FallbackReason!=null;
            item.Fallback.Text=(item.TransferKind=="full"?"改用完整更新：":"回退说明：")+FallbackText(item.FallbackReason);
            item.Download.IsIndeterminate=item.Total is not >0;
            item.Download.Value=Percent(item.Received,item.Total);
            item.Bytes.Text="本项接收："+SizeProgress(item.Received,item.Total);
            var current=ReferenceEquals(item,active);var idle=item.Idle+(current?extra:0);
            var speed=current&&idle>=8?0:item.Speed;
            item.Rate.Text=(item.Stage is "connecting" or "downloading")&&!done&&!bad
                ?$"速度：{Size(speed)}/s · 本项剩余：{Eta(speed>0?item.Eta:null)}":"";
            item.Last.Text=item.LastReceived is {} last?"最后收到数据："+last.ToLocalTime().ToString("HH:mm:ss"):"尚未收到网络数据";
            item.Verification.IsVisible=item.VerificationStep!=null;
            item.Verify.IsIndeterminate=item.VerificationTotal is not >0;
            item.Verify.Value=Percent(item.VerificationBytes,item.VerificationTotal);
            item.VerifyText.Text=VerificationText(item.VerificationStep)+"："+SizeProgress(item.VerificationBytes,item.VerificationTotal);
        }
    }
    public void End(Guid localInvocation,JObject result)
    {
        if(invocation!=localInvocation||!IsActive)return;
        Render();IsActive=false;wait.Text="";
        var partial=result["pacer_warning"] is JObject||(bool?)result["component"]?["ok"]==false;
        HasFailed=(bool?)result["ok"]!=true||partial;
        stage.Text=HasFailed?partial?"部分内容未准备完成，请查看本次结果。":"更新准备未完成，可以重新尝试。":
            (string?)result["status"] is "current" or "no_update"?"检查完成，当前无需下载更新。":
            (string?)result["status"]=="prepared"||(string?)result["component"]?["status"]=="prepared"?"更新文件已准备，安装状态以本次结果为准。":"本次操作已完成，请查看结果。";
        if(HasFailed&&MaaUmaUpdateConnectivity.FailureText(result) is { Length:>0 } networkFailure)stage.Text=networkFailure;
        if(items.Count==0){overall.IsVisible=false;overallBytes.IsVisible=false;counts.IsVisible=false;}
        else {overall.IsIndeterminate=false;overall.IsVisible=overallTotal is >0;}
        foreach(var item in items.Values){item.Download.IsIndeterminate=false;item.Verify.IsIndeterminate=false;item.Rate.Text="";
            item.Download.IsVisible=item.Total is >0;item.Verify.IsVisible=item.VerificationTotal is >0;
            if(item.VerificationTotal is not >0&&item.VerificationStep!=null)
                item.VerifyText.Text=item.Status=="verified"?VerificationText(item.VerificationStep)+"：已完成":"本次校验结果以最终响应为准。";
            if(item.Status=="active")item.Phase.Text="本次操作已结束，保留最后收到的进度记录。";}
    }
    public void Fail(Guid localInvocation)
    {
        if(invocation!=localInvocation||!IsActive)return;
        IsActive=false;HasFailed=true;stage.Text="本次下载结果未确认，可以重新尝试；不会自动重试。";wait.Text="";overall.IsIndeterminate=false;
        overall.IsVisible=overallTotal is >0;
        foreach(var item in items.Values){item.Download.IsIndeterminate=false;item.Verify.IsIndeterminate=false;item.Rate.Text="";
            item.Download.IsVisible=item.Total is >0;item.Verify.IsVisible=item.VerificationTotal is >0;
            if(item.Status=="active")item.Phase.Text="本项结果未确认，保留最后收到的进度记录。";}
    }
    public void Clear(){IsActive=false;HasFailed=false;IsVisible=false;invocation=Guid.Empty;items.Clear();rows.Children.Clear();}
    private static double Percent(double received,double? total)=>total is >0?Math.Clamp(received/total.Value*100,0,100):0;
    private static string Size(double bytes){var units=new[]{"B","KiB","MiB","GiB","TiB"};var unit=0;while(bytes>=1024&&unit<units.Length-1){bytes/=1024;unit++;}return bytes.ToString(unit==0?"0":"0.0",CultureInfo.InvariantCulture)+" "+units[unit];}
    private static string SizeProgress(double bytes,double? total)=>Size(bytes)+(total is {} known?" / "+Size(known)+(known>0?$"（{bytes/known*100:0.0}%）":""):" / 总大小未知");
    private static string Eta(double? seconds){if(seconds is not {} value)return "暂无法估算";var span=TimeSpan.FromSeconds(Math.Min(value,TimeSpan.MaxValue.TotalSeconds-1));return span.TotalHours>=1?$"约 {span.TotalHours:0} 小时":span.TotalMinutes>=1?$"约 {(int)span.TotalMinutes} 分 {span.Seconds} 秒":$"约 {Math.Ceiling(value):0} 秒";}
    private static string StageText(string value)=>value switch {"checking"=>"正在检查可用更新", "connecting"=>"正在连接下载服务", "downloading"=>"正在下载", "verifying"=>"正在校验本地文件内容", "preparing"=>"正在准备安装文件", "prepared"=>"文件已准备，正在确认最终结果", "current"=>"当前项目无需更新", "failed"=>"本次准备未完成", _=>"正在等待进度"};
    private static string FallbackText(string? value)=>value switch {"no_patch"=>"没有适用的增量包。","local_state_mismatch"=>"本机文件与增量更新基础不一致。","patch_unusable"=>"增量包无法使用。",_=>""};
    private static string VerificationText(string? value)=>value switch {"archive_integrity"=>"压缩包完整性校验","file_integrity"=>"本地文件校验（含复用文件，不消耗下载流量）","personalized_integrity"=>"分发包完整性校验","archive_metadata"=>"压缩包信息核验","receipt"=>"分发凭据核验",_=>"校验"};
}
