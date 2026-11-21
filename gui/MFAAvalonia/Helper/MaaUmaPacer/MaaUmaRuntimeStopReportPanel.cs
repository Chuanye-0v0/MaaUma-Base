using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Threading;
using Avalonia.Threading;
using UmaCutPanel;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>One user-opened RuntimeStop draft; previews only backend-bound saved game pixels.</summary>
public sealed class MaaUmaRuntimeStopReportPanel : StackPanel, IDisposable
{
    private readonly string packageRoot;
    private readonly Func<JObject,Task<JObject>> request;
    private readonly Func<JObject,Action<JObject>?,CancellationToken,Task<JObject>> upload;
    private CancellationTokenSource? uploadCancellation;
    private readonly Button cancelUpload=new(){Name="PacerCancelStopReportUpload",Content=Text("取消上传"),IsVisible=false};
    private readonly TextBlock status=Text("正在读取本次停止的本地留档；尚未发送报告。");
    private readonly TextBlock imageNotice=Text("没有读取当前画面或桌面。");
    private readonly Image image=new() {Name="PacerStopSavedImage",Height=320,MaxWidth=520,
        Stretch=Stretch.Uniform,HorizontalAlignment=HorizontalAlignment.Center,IsVisible=false};
    private readonly CheckBox include=new() {Name="PacerStopIncludeScreenshot",Content=Text("附上这张留档游戏截图（仅维护者可见）"),
        IsChecked=false,IsEnabled=false};
    private readonly Button zoom=new() {Name="PacerStopEnlargeScreenshot",Content=Text("放大留档图"),IsEnabled=false};
    private readonly TextBox preview=new() {Name="PacerStopReportPreviewText",IsReadOnly=true,
        AcceptsReturn=true,TextWrapping=TextWrapping.Wrap,MinHeight=100,MaxHeight=180,
        Text="正在准备脱敏停止摘要……"};
    private readonly Button submit=new() {Name="MaaUmaSubmitPrivateError",Content=Text("私密提交停止报告")};
    private readonly Button copy=new() {Name="PacerCopyStopReport",Content=Text("复制脱敏报告")};
    private JObject? context;
    private string mode="career",runId="",message="",details="",imageIssue="";
    private string? reportId,previewText,feedbackId;
    private Bitmap? bitmap;
    private Window? zoomWindow;
    private bool busy,submitted,networkAttempted,loading,imageAvailable,imageAttachable,disposed;

    public MaaUmaRuntimeStopReportPanel(string packageRoot,Func<JObject,Task<JObject>> request):this(packageRoot,request,null){}
    public MaaUmaRuntimeStopReportPanel(string packageRoot,Func<JObject,Task<JObject>> request,
        Func<JObject,Action<JObject>?,CancellationToken,Task<JObject>>? upload)
    {
        this.packageRoot=Path.GetFullPath(packageRoot);this.request=request;
        this.upload=upload??((value,progress,token)=>new CutServiceClient(Path.Combine(this.packageRoot,"pacer")).Feedback(value,progress,token));
        cancelUpload.Click+=(_,_)=>uploadCancellation?.Cancel();
        Name="PacerRuntimeStopReportPanel";Spacing=10;
        status.Name="PacerStopReportStatus";imageNotice.Name="PacerStopScreenshotNotice";
        Children.Add(status);Children.Add(imageNotice);Children.Add(image);Children.Add(zoom);Children.Add(include);
        var textPreview=new Expander {Header=Text("脱敏文字预览"),Content=preview,IsExpanded=true};
        Children.Add(textPreview);
        Children.Add(new WrapPanel {Children={submit,copy,cancelUpload}});
        foreach(var control in new TemplatedControl[] {include,zoom,preview,submit,copy,textPreview})
            control.Bind(TemplatedControl.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        preview.Bind(TemplatedControl.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        submit.Margin=new Thickness(0,0,10,0);
        submit.Click+=async(_,_)=>await SubmitCurrentAsync();
        copy.Click+=async(_,_)=> {
            if(TopLevel.GetTopLevel(this)?.Clipboard is { } clipboard)await CopyCurrentAsync(clipboard.SetTextAsync);
        };
        zoom.Click+=(_,_)=> {
            if(bitmap==null)return;
            zoomWindow?.Close();
            zoomWindow=new Window {Title="本次停止的留档游戏画面",Width=500,Height=850,
                WindowStartupLocation=WindowStartupLocation.CenterOwner,
                Content=new Image {Source=bitmap,Stretch=Stretch.Uniform}};
            zoomWindow.Bind(TemplatedControl.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
            if(TopLevel.GetTopLevel(this) is Window owner&&owner.IsVisible)zoomWindow.Show(owner);else zoomWindow.Show();
        };
        include.PropertyChanged+=(_,args)=> {
            if(args.Property!=ToggleButton.IsCheckedProperty || loading || busy || networkAttempted)return;
            reportId=previewText=null;
            preview.Text="附图选择已更新；复制或提交时会重新准备脱敏报告。";
            status.Text="尚未发送。将按当前勾选准备本次报告；仅维护者可见。";
        };
        Buttons();
    }

    private static TextBlock Text(string value)
    {
        var text=new TextBlock {Text=value,TextWrapping=TextWrapping.Wrap};
        text.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiText"));return text;
    }
    private static string S(JToken? token,int limit=300)
    {
        if(token?.Type!=JTokenType.String)return "";
        var value=token.Value<string>()??"";return value.Length<=limit?value:value[..limit];
    }
    private static string Limited(string? value,int limit)=>value==null?"":value.Length<=limit?value:value[..limit];
    private static string MissingImage(JObject value)
    {
        var reason=S(value["image_unavailable_reason"],160);
        var explanation=reason switch {
            "empty_frame"=>"截图接口没有返回有效画面",
            "timeout"=>"截图等待超时",
            "decode_failed"=>"截图无法解码",
            "write_failed"=>"截图未能保存",
            "connection_failed" or "native_unavailable" or "command_failed"=>"截图连接未成功",
            "diagnostic_saved_frame_scope_mismatch" or "diagnostic_saved_segment_unavailable"=>"没有可确认属于本次育成段的留档图",
            "diagnostic_saved_frame_not_current"=>"已有留档图不属于本次育成段的时间范围",
            _=>"本次没有可用的已存游戏画面"
        };
        return explanation+"；本次仅提交文字，可在下方摘要查看已记录的停止原因。";
    }
    private static string Time(JToken? value)=>DateTimeOffset.TryParse(value?.Type==JTokenType.Date
        ? value.ToString(Newtonsoft.Json.Formatting.None).Trim('"') : S(value),out var date)
        ? date.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss") : "未记录";
    private void Buttons()
    {
        cancelUpload.IsVisible=uploadCancellation!=null;cancelUpload.IsEnabled=busy&&!disposed&&uploadCancellation!=null;
        submit.IsEnabled=!busy&&!submitted&&!disposed;
        copy.IsEnabled=!busy&&!disposed;
        include.IsEnabled=imageAttachable&&!busy&&!networkAttempted&&!disposed;
        zoom.IsEnabled=imageAvailable&&!disposed;
    }
    public void SetReport(string mode,string? runId,string? message,string? details)
    {
        if(busy||networkAttempted)return;
        this.mode=mode;this.runId=runId??"";this.message=Limited(message,8000);this.details=Limited(details,24000);
        reportId=previewText=feedbackId=null;context=null;submitted=false;imageIssue="";
        imageAvailable=imageAttachable=false;image.IsVisible=false;image.Source=null;bitmap?.Dispose();bitmap=null;
        loading=true;include.IsChecked=false;loading=false;Buttons();
    }
    private string AppVersion()
    {
        try {
            var value=S(JObject.Parse(File.ReadAllText(Path.Combine(packageRoot,"interface.json"),Encoding.UTF8))["version"],64);
            return Regex.IsMatch(value,@"\A[0-9A-Za-z.+_-]{1,64}\z")?value:"unknown";
        }catch{return "unknown";}
    }

    public async Task LoadEvidenceAsync()
    {
        if(busy||networkAttempted||disposed)return;
        busy=true;Buttons();
        try {
            var result=await request(new JObject {["action"]="runtime_stop_evidence",["mode"]=mode,["run_id"]=runId});
            if(disposed)return;
            context=(result["context"] as JObject)?.DeepClone() as JObject;
            if((bool?)result["ok"]==true && S(result["status"])=="available" && result["image"] is JObject saved) {
                var encoded=saved["data_base64"]?.Type==JTokenType.String?saved["data_base64"]!.Value<string>()??"":"";
                var data=Convert.FromBase64String(encoded);
                if(S(saved["media_type"])!="image/jpeg" || data.Length<=0
                    || data.Length<3 || data[0]!=0xff || data[1]!=0xd8 || data[2]!=0xff
                    || !string.Equals(Convert.ToHexString(SHA256.HashData(data)),S(saved["sha256"],64),StringComparison.OrdinalIgnoreCase))
                    throw new IOException();
                using var stream=new MemoryStream(data,false);
                var candidate=new Bitmap(stream);
                if(candidate.PixelSize.Width!=(int?)saved["width"] || candidate.PixelSize.Height!=(int?)saved["height"]
                    || candidate.PixelSize.Width<=0 || candidate.PixelSize.Height<=0) {candidate.Dispose();throw new IOException();}
                bitmap=candidate;image.Source=bitmap;image.IsVisible=true;imageAvailable=imageAttachable=true;
                var cached=S(context?["frame_scope"]).Contains("before",StringComparison.OrdinalIgnoreCase);
                var relation=S(context?["frame_relation"]);
                var historical=relation is "historical_context" or "legacy_unverified";
                var age=(double?)context?["frame_age_seconds"];
                var warning=S(context?["frame_warning"]) switch {
                    "turn_changed_since_frame"=>"画面采集后脚本回合序号已变化，无法对应停止步骤。",
                    "earlier_observation"=>"这是停止前已观察的画面，不能据此确认停止瞬间页面。",
                    "capture_binding_not_recorded"=>"旧留档没有连续观察来源，无法确认停止步骤。",
                    "owner_turn_not_recorded"=>"画面对应的脚本回合序号未记录。",
                    _=>""
                };
                imageNotice.Text=(historical?"历史游戏画面，仅作上下文；不是停机现场。":
                    cached?"停止前最近已观察的游戏画面（非实时；页面可能已变化）。":"当时保存的游戏画面。")+
                    "\n停止记录："+Time(context?["stop_recorded_at"])+"；画面采集："+Time(context?["captured_at"])+
                    (age.HasValue?"\n画面早于停止记录 "+age.Value.ToString("0.###")+" 秒。":"")+
                    (warning.Length>0?"\n"+warning:"")+
                    "\n局号："+S(context?["run_id"],160)+"；停止原因："+S(context?["stop_reason"],160)+
                    "\n此图来自已存的游戏画面，不会截取当前游戏或桌面。";
                loading=true;include.IsChecked=!historical&&(bool?)result["include_screenshot_default"]==true;loading=false;
            } else {
                imageNotice.Text=MissingImage(result);
            }
        }catch {
            imageIssue="preview_failed";
            imageAvailable=imageAttachable=false;image.Source=null;image.IsVisible=false;bitmap?.Dispose();bitmap=null;
            loading=true;include.IsChecked=false;loading=false;
            imageNotice.Text="留档图暂未读取或未能核对，本次可仅提交文字；不会采现在的画面替代。";
        }
        try {await PrepareAsync();}
        catch {status.Text="脱敏摘要暂未准备好，可重试复制或私密提交。";}
        finally {busy=false;Buttons();}
    }

    private async Task<bool> PrepareAsync()
    {
        if(reportId!=null&&previewText!=null)return true;
        var wanted=imageAttachable&&include.IsChecked==true;
        var response=await request(new JObject {["action"]="prepare_runtime_stop_report",["mode"]=mode,["run_id"]=runId,
            ["context"]=context?.DeepClone(),["include_screenshot"]=wanted,["app_version"]=AppVersion(),
            ["message"]=message,["stack_trace"]=details});
        var count=(int?)response["screenshot_count"];
        if((bool?)response["ok"]!=true || !Guid.TryParseExact(S(response["report_id"]),"D",out var id)
            || response["preview_text"]?.Type!=JTokenType.String || count is not (0 or 1)
            || !wanted&&count==1)throw new IOException();
        var text=S(response["preview_text"],65537);
        if(string.IsNullOrWhiteSpace(text)||text.Length>65536)throw new IOException();
        reportId=id.ToString("D");previewText=text;preview.Text=text;
        if(count==0&&wanted) {
            loading=true;include.IsChecked=false;loading=false;imageAttachable=false;
            if(S(response["attachment_status"])=="stop_context_changed")imageIssue="context_changed";
            imageNotice.Text=S(response["attachment_status"])=="stop_context_changed"
                ? "上方仅为已读取的本地留档预览。附图核对未通过，本次报告仅提交文字；不会使用新画面替代。"
                : MissingImage(response);
        }
        var missing=imageIssue=="preview_failed"?"留档图读取或预览未通过，本次仅提交文字。"
            :imageIssue=="context_changed"?"本次留档已变化，未附上方预览图，仅提交文字。":MissingImage(response);
        status.Text=count==1?"已准备脱敏摘要和上方留档截图，尚未发送；仅维护者可见。"
            : imageAttachable?"已准备脱敏文字报告；本次未勾选附图，尚未发送。"
            : "已准备脱敏文字报告，未附图，尚未发送。"+missing;
        return true;
    }

    public async Task SubmitCurrentAsync()
    {
        if(busy||submitted||disposed)return;
        busy=true;Buttons();
        try {
            await PrepareAsync();networkAttempted=true;Buttons();
            status.Text="正在私密提交本次停止报告……";
            uploadCancellation=new CancellationTokenSource();Buttons();
            var response=await upload(new JObject {["action"]="submit_runtime_stop_report",["report_id"]=reportId},entry=>Dispatcher.UIThread.Post(()=>{
                if(!disposed&&uploadCancellation?.IsCancellationRequested==false)status.Text=$"正在上传 {entry["completed_parts"]} / {entry["total_parts"]} 段……";
            }),uploadCancellation.Token);
            if((bool?)response["ok"]==false){submit.Content=Text("重试私密提交");status.Text=S(response["message"],400);if(string.IsNullOrWhiteSpace(status.Text))status.Text="报告及所选截图尚未确认提交，原报告和分段进度保留，可重试。";return;}
            var feedback=S(response["feedback_id"]);
            if((bool?)response["ok"]!=true || !Regex.IsMatch(feedback,@"\ABF-[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}\z",RegexOptions.IgnoreCase))
                throw new IOException();
            feedbackId=feedback;submitted=true;submit.Content=Text("已私密提交");
            var actualCount=response["screenshot_count"]?.Type==JTokenType.Integer?(int?)response["screenshot_count"]:null;
            var attachment=actualCount switch {1=>"已随报告提交留档游戏截图。",0=>"本次仅提交文字，未附图。",_=>"附图状态未确认，可凭编号核对。"};
            status.Text="服务端已确认报告，反馈编号："+feedback+"。"+attachment+"仅维护者可见。";
        }catch(OperationCanceledException){submit.Content=Text("重试私密提交");status.Text="上传已取消，原报告和分段进度保留，可重试同一次提交。";}
        catch {submit.Content=Text("重试私密提交");status.Text="提交未确认，可重试或复制脱敏报告；本次已准备的内容会保留。";}
        finally {uploadCancellation?.Dispose();uploadCancellation=null;busy=false;Buttons();}
    }
    public async Task CopyCurrentAsync(Func<string,Task> copyText)
    {
        if(busy||disposed)return;
        busy=true;Buttons();
        try {
            await PrepareAsync();await copyText(previewText!);
            status.Text="已复制脱敏文字报告。"+(feedbackId!=null?"反馈编号："+feedbackId:"");
        }catch {status.Text="脱敏报告暂未准备好，可重试复制或私密提交。";}
        finally {busy=false;Buttons();}
    }
    public void Dispose()
    {
        if(disposed)return;disposed=true;uploadCancellation?.Cancel();zoomWindow?.Close();zoomWindow=null;
        image.Source=null;bitmap?.Dispose();bitmap=null;Buttons();
    }
}
