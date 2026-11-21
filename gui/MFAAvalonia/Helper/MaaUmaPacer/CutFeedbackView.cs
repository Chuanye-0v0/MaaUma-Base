using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Threading;
using Avalonia.Threading;
using System.Diagnostics;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

internal sealed class CutFeedbackView:UserControl
{
    private readonly ICutServiceClient client;
    private readonly string root;
    private readonly TextBlock selected=new() {Name="FeedbackSelectedRun",Text="先在复盘列表选择一局。",TextWrapping=TextWrapping.Wrap};
    private readonly TextBox description=new() {Name="FeedbackDescription",AcceptsReturn=true,MinHeight=76,MaxHeight=150,TextWrapping=TextWrapping.Wrap,Watermark="说明哪里出了问题，以及希望怎样处理（最多4000字）",MaxLength=4000};
    private readonly Button preview=new() {Name="PrepareFeedback",Content="生成上传预览",IsEnabled=false};
    private readonly TextBlock summary=new() {Name="FeedbackSummary",TextWrapping=TextWrapping.Wrap};
    private readonly CheckBox includeImage=new() {Name="IncludeStopScreenshot",Content="附上本局异常停机游戏截图（如有，可在下方预览）",IsChecked=true};
    private readonly Image stopImage=new() {Name="FeedbackStopImage",MaxHeight=360,Stretch=Stretch.Uniform};
    private readonly Expander imagePanel=new() {Name="FeedbackStopImagePanel",Header="查看将要上传的停机截图",IsVisible=false};
    private readonly TextBox detail=new() {Name="FeedbackPreview",IsReadOnly=true,AcceptsReturn=true,TextWrapping=TextWrapping.Wrap,MinHeight=100,MaxHeight=260};
    private readonly Button full=new() {Name="OpenFeedbackPreview",Content="打开完整脱敏预览",IsEnabled=false};
    private readonly CheckBox confirmed=new() {Name="ConfirmFeedback",Content="我已查看预览，同意将这份文字复盘和问题说明提交给维护者",IsEnabled=false};
    private readonly Button submit=new() {Name="SubmitFeedback",Content="确认提交",IsEnabled=false};
    private readonly Button query=new() {Name="CheckFeedbackStatus",Content="查看处理状态",IsEnabled=false};
    private readonly TextBlock receipt=new() {Name="FeedbackReceipt",TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock message=new() {Name="FeedbackMessage",Text="不自动上传。可附上异常停机时保存的游戏画面，不截取电脑桌面；服务未配置时仍可本地导出。",TextWrapping=TextWrapping.Wrap};
    private string? runId,draftId,draftRun,draftDescription,previewPath,feedbackId;
    private bool busy,activated;
    private string? pendingRun,pendingTitle,currentTitle;
    private bool hasPendingSelection;
    private readonly Button discardDraft=new(){Name="DiscardFeedbackDraft",Content="放弃当前预览并切换所选局",IsVisible=false};
    private CancellationTokenSource? uploadCancellation;
    private readonly Button cancelUpload=new(){Name="CancelFeedbackUpload",Content="取消上传",IsVisible=false};

    public CutFeedbackView(ICutServiceClient client,string root)
    {
        this.client=client;this.root=root;
        cancelUpload.Click+=(_,_)=>uploadCancellation?.Cancel();DetachedFromVisualTree+=(_,_)=>uploadCancellation?.Cancel();
        var body=new StackPanel {Spacing=9};
        body.Children.Add(selected);body.Children.Add(description);body.Children.Add(includeImage);body.Children.Add(preview);body.Children.Add(summary);
        imagePanel.Content=stopImage;body.Children.Add(imagePanel);
        body.Children.Add(new Expander {Name="FeedbackDetails",Header="查看具体上传内容",Content=detail,HorizontalContentAlignment=HorizontalAlignment.Stretch});
        body.Children.Add(full);
        confirmed.Content=new TextBlock {Text="我已查看预览，同意将这份复盘、问题说明及选中的停机截图提交给维护者",TextWrapping=TextWrapping.Wrap};
        body.Children.Add(confirmed);
        var actions=new WrapPanel {Orientation=Orientation.Horizontal};
        submit.Margin=new Thickness(0,0,10,0);actions.Children.Add(submit);actions.Children.Add(cancelUpload);actions.Children.Add(discardDraft);actions.Children.Add(query);
        body.Children.Add(actions);body.Children.Add(receipt);body.Children.Add(message);Content=body;
        description.TextChanged+=(_,_)=>{if(draftId!=null && description.Text!=draftDescription)Invalidate();else Buttons();};
        confirmed.IsCheckedChanged+=(_,_)=>Buttons();
        includeImage.IsCheckedChanged+=(_,_)=>Invalidate();
        discardDraft.Click+=(_,_)=>{if(busy)return;Invalidate();feedbackId=null;receipt.Text="";
            if(hasPendingSelection){runId=pendingRun;currentTitle=pendingTitle;hasPendingSelection=false;}
            selected.Text=currentTitle==null?"先在复盘列表选择一局。":"选中局："+currentTitle;Buttons();};
        preview.Click+=async(_,_)=>await Prepare();
        submit.Click+=async(_,_)=>await Submit();
        query.Click+=async(_,_)=>await Status();
        full.Click+=(_,_)=> {
            try {if(previewPath!=null && File.Exists(previewPath))Process.Start(new ProcessStartInfo(previewPath) {UseShellExecute=true});}
            catch(Exception){message.Text="无法打开预览文件；可在下方展开具体内容。";}
        };
    }

    public void SetRun(string? id,string? title)
    {
        if(busy || draftId!=null){
            if(runId==id){hasPendingSelection=false;pendingRun=pendingTitle=null;Buttons();return;}
            if(runId!=id){pendingRun=id;pendingTitle=title;hasPendingSelection=true;message.Text="当前预览仍属于原选中局；上传结果和重试保留在这里。放弃预览后可切换到列表新选择。";Buttons();}
            return;
        }
        hasPendingSelection=false;pendingRun=pendingTitle=null;currentTitle=title;selected.Text=title==null ? "先在复盘列表选择一局。" : "选中局："+title;
        if(runId==id)return;
        runId=id;Invalidate();
    }
    private void Invalidate()
    {
        if(busy)return;
        draftId=draftRun=draftDescription=previewPath=null;
        (stopImage.Source as IDisposable)?.Dispose();stopImage.Source=null;imagePanel.IsVisible=false;
        summary.Text=detail.Text="";confirmed.IsChecked=false;activated=false;Buttons();
    }
    private void Buttons()
    {
        cancelUpload.IsVisible=uploadCancellation!=null;cancelUpload.IsEnabled=busy&&uploadCancellation!=null;
        discardDraft.IsVisible=draftId!=null||hasPendingSelection;discardDraft.IsEnabled=!busy;
        description.IsEnabled=!busy;
        includeImage.IsEnabled=!busy;
        preview.IsEnabled=!busy && runId!=null && !string.IsNullOrWhiteSpace(description.Text);
        full.IsEnabled=!busy && previewPath!=null;
        confirmed.IsEnabled=!busy && draftId!=null && activated;
        submit.IsEnabled=!busy && activated && draftId!=null && runId==draftRun && description.Text==draftDescription && confirmed.IsChecked==true;
        query.IsEnabled=!busy && feedbackId!=null;
    }
    private void Busy(bool value){busy=value;Buttons();}
    private async Task Prepare()
    {
        if(busy || runId==null || string.IsNullOrWhiteSpace(description.Text))return;
        var id=runId;var text=description.Text;Invalidate();Busy(true);message.Text="正在生成本机复盘预览，尚未上传……";
        try {
            var result=await client.Request(new JObject {["action"]="prepare_feedback",["run_id"]=id,["description"]=text,["include_screenshot"]=includeImage.IsChecked==true});
            if(runId!=id || description.Text!=text){message.Text="选中局或说明已改变，请重新生成预览。";return;}
            if((bool?)result["ok"]!=true){message.Text=(string?)result["message"] ?? "预览未生成。";return;}
            var draft=(string?)result["draft_id"];
            if(string.IsNullOrWhiteSpace(draft))throw new IOException();
            draftId=draft;draftRun=id;draftDescription=text;
            summary.Text=((string?)result["summary"] ?? "文字复盘预览")+$"\n压缩后约 {Math.Ceiling(((double?)result["size_bytes"] ?? 0)/1024):0} KB；此处尚未上传。";
            detail.Text=(string?)result["preview_text"] ?? "完整内容请打开脱敏预览文件查看。";
            var path=(string?)result["preview_path"];
            var allowed=Path.GetFullPath(Path.Combine(root,"exports","feedback","drafts"))+Path.DirectorySeparatorChar;
            if(!string.IsNullOrWhiteSpace(path) && Path.GetFullPath(path).StartsWith(allowed,StringComparison.OrdinalIgnoreCase) && File.Exists(path))previewPath=Path.GetFullPath(path);
            var imagePath=(string?)result["image_preview_path"];
            if((int?)result["screenshot_count"]==1) {
                if(string.IsNullOrWhiteSpace(imagePath) || !Path.GetFullPath(imagePath).StartsWith(allowed,StringComparison.OrdinalIgnoreCase) || !File.Exists(imagePath))throw new IOException();
                stopImage.Source=new Bitmap(imagePath);imagePanel.IsVisible=true;
            }
            var info=await client.Request(new JObject {["action"]="service_info"});
            activated=(bool?)info["ok"]==true && (bool?)info["activated"]==true;
            message.Text=activated ? "请查看预览，再勾选确认并提交。内容仅维护者可见。" : "预览已保存在本机，尚未上传。请在“更新与服务”配置地址并激活；也可继续使用本地导出。";
        } catch(Exception){Invalidate();message.Text="预览未完成，没有上传。可稍后重试或本地导出。";}
        finally {Busy(false);}
    }
    private async Task Submit()
    {
        if(busy || !activated || confirmed.IsChecked!=true || draftId==null || runId!=draftRun || description.Text!=draftDescription)return;
        var draft=draftId;uploadCancellation=new CancellationTokenSource();Busy(true);message.Text="正在提交已确认的预览……";
        try {
            var result=await client.Feedback(new JObject {["action"]="submit_feedback",["draft_id"]=draft},entry=>Dispatcher.UIThread.Post(()=>{
                if(busy&&uploadCancellation?.IsCancellationRequested==false)message.Text=$"正在上传 {entry["completed_parts"]} / {entry["total_parts"]} 段……";
            }),uploadCancellation.Token);
            if((bool?)result["ok"]!=true){message.Text=(string?)result["message"] ?? "提交未确认，请重试同一份预览。";return;}
            var id=(string?)result["feedback_id"];
            if(string.IsNullOrWhiteSpace(id))throw new IOException();
            feedbackId=id;receipt.Text="反馈编号："+id+" · 局："+(currentTitle??draftRun)+" · "+StatusLabel((string?)result["feedback_status"]);
            draftId=null;confirmed.IsChecked=false;message.Text="已提交，仅维护者可以读取。原记录和本地导出仍保留。";
        } catch(OperationCanceledException){message.Text="上传已取消，原预览与分段进度保留，可重试同一份。";}
        catch(Exception){message.Text="没有取得提交确认；可重试同一份预览，服务会避免重复提交。";}
        finally {uploadCancellation?.Dispose();uploadCancellation=null;Busy(false);}
    }
    private async Task Status()
    {
        if(busy || feedbackId==null)return;Busy(true);
        try {
            var result=await client.Request(new JObject {["action"]="feedback_status",["feedback_id"]=feedbackId});
            if((bool?)result["ok"]!=true){message.Text=(string?)result["message"] ?? "暂时无法查询。";return;}
            receipt.Text="反馈编号："+feedbackId+" · "+StatusLabel((string?)result["feedback_status"]);
            if((string?)result["fixed_version"] is string version && version.Length>0)receipt.Text+=" · 修复版本："+version;
            message.Text=(string?)result["note"] ?? "处理状态已刷新。";
        } catch(Exception){message.Text="暂时无法查询处理状态；反馈编号保留。";}
        finally {Busy(false);}
    }
    private static string StatusLabel(string? status)=>status switch {"open"=>"待查看","triaged"=>"已确认问题","fixed"=>"已修复","closed"=>"已关闭",_=>"状态待确认"};
}
