using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using MFAAvalonia.Helper;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

internal sealed class CutUpdateView:UserControl
{
    private readonly ICutServiceClient client;
    private readonly Func<JObject,Task> install;
    private readonly TextBox address=new() {Name="CutServiceAddress",Watermark="维护者提供的 HTTPS 服务地址"};
    private readonly ComboBox channel=new() {Name="CutUpdateChannel",ItemsSource=new[]{"测试版","稳定版"},SelectedIndex=0,MinWidth=160};
    private readonly TextBox invite=new() {Name="CutServiceInvite",Watermark="邀请码（激活成功后清空）",PasswordChar='●'};
    private readonly Button save=new() {Name="CutSaveService",Content="保存服务与频道"};
    private readonly Button activate=new() {Name="CutActivateService",Content="激活邀请码"};
    private readonly Button check=new() {Name="CutCheckUpdate",Content="检查更新"};
    private readonly Button download=new() {Name="CutDownloadUpdate",Content="下载更新",IsEnabled=false};
    private readonly Button apply=new() {Name="CutInstallUpdate",Content="停止后安装并重开",IsEnabled=false};
    private readonly Button cancelDownload=new() {Name="CutCancelDownload",Content="取消下载",IsVisible=false};
    private readonly ProgressBar downloadProgress=new() {Name="CutDownloadProgress",Minimum=0,Maximum=100,Height=8,IsVisible=false};
    private readonly TextBlock serviceStatus=new() {Name="CutServiceStatus",Text="服务未配置。运行前需激活本机；本地复盘导出仍可使用。",TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock message=new() {Name="CutUpdateStatus",TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock lastInstall=new() {Name="CutLastInstall",Text="上次安装：尚无记录。",TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock notes=new() {Name="CutUpdateNotes",TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock distribution=new() {Name="CutUpdateDistribution",Text="来源编号：未记录",TextWrapping=TextWrapping.Wrap};
    public event Action<JObject>? ServiceInfoChanged;
    private JObject? prepared;
    private bool busy,active,available;
    private string savedAddress="";
    private int savedChannel;
    private bool ConfigurationDirty=>(address.Text ?? "").Trim()!=savedAddress || channel.SelectedIndex!=savedChannel;
    private string runtimeState="unknown";
    private bool hasActivationReceipt;
    private CancellationTokenSource? downloadCancellation;
    private Task<JObject>? downloadOperation;

    public CutUpdateView(ICutServiceClient client,Func<JObject,Task> install,string defaultChannel="preview")
    {
        this.client=client;this.install=install;
        channel.SelectedIndex=defaultChannel=="stable" ? 1 : 0;savedChannel=channel.SelectedIndex;
        var body=new StackPanel {Margin=new Thickness(20),Spacing=12};
        body.Children.Add(new TextBlock {Text="更新与服务",FontSize=22});
        body.Children.Add(new TextBlock {Text="本地整合候选不安装独立刷切补丁。整包更新由主面板入口管理，当前未配置更新源。",TextWrapping=TextWrapping.Wrap,Opacity=.75});
        body.Children.Add(new TextBlock {Text="当前为开源基础版：运行育成无需授权验证或激活码；复盘仅在确认后上传，扩展包与更新均由你手动放行或安装。",TextWrapping=TextWrapping.Wrap,Opacity=.75});
        body.Children.Add(new TextBlock {Name="CutDeviceLicenseHelp",Text="开源版不检查设备绑定或运行期邀请码，停止/暂停后可随时继续当前育成。若需下载可选更新或社区方案，可按需填写服务地址。",TextWrapping=TextWrapping.Wrap,Opacity=.75});
        body.Children.Add(distribution);
        ToolTip.SetTip(distribution,"公开编号用于分发来源核对，不是邀请码；编号本身不证明转发者身份。复盘仅标记本次导出来源。");
        body.Children.Add(new TextBlock {Text="服务地址"});body.Children.Add(address);
        body.Children.Add(new TextBlock {Text="更新频道"});body.Children.Add(channel);body.Children.Add(save);
        body.Children.Add(new TextBlock {Text="邀请码"});body.Children.Add(invite);body.Children.Add(activate);body.Children.Add(serviceStatus);
        var buttons=new WrapPanel {Orientation=Orientation.Horizontal};
        foreach(var button in new[]{check,download,apply,cancelDownload}){button.Margin=new Thickness(0,0,10,8);buttons.Children.Add(button);}
        body.Children.Add(buttons);body.Children.Add(downloadProgress);body.Children.Add(lastInstall);body.Children.Add(message);body.Children.Add(notes);
        Content=new ScrollViewer {Content=body};
        Loaded+=async(_,_)=>await LoadInfo();
        save.Click+=async(_,_)=>await Configure();
        activate.Click+=async(_,_)=>await Activate();
        check.Click+=async(_,_)=>await Update("check");
        download.Click+=async(_,_)=>await Update("prepare");
        apply.Click+=async(_,_)=>await Install();
        cancelDownload.Click+=(_,_)=>{
            if(downloadCancellation is not { } cancellation)return;
            message.Text="正在取消下载……";cancelDownload.IsEnabled=false;cancellation.Cancel();
        };
        address.TextChanged+=(_,_)=>InvalidateDownload();
        channel.SelectionChanged+=(_,_)=>InvalidateDownload();
    }

    public void SetRuntimeState(string? state){runtimeState=state ?? "unknown";Buttons();}
    private void InvalidateDownload(){prepared=null;available=false;Buttons();}
    private void Buttons()
    {
        address.IsEnabled=channel.IsEnabled=invite.IsEnabled=save.IsEnabled=!busy;
        activate.IsEnabled=!busy && !ConfigurationDirty;check.IsEnabled=!busy && active && !ConfigurationDirty;
        download.IsEnabled=!busy && active && available && !ConfigurationDirty;
        apply.IsEnabled=!busy && prepared!=null && runtimeState is "idle" or "stopped";
        cancelDownload.IsVisible=downloadProgress.IsVisible=busy && downloadCancellation!=null;
        cancelDownload.IsEnabled=cancelDownload.IsVisible && downloadCancellation?.IsCancellationRequested==false;
        check.IsEnabled=download.IsEnabled=apply.IsEnabled=false;
        invite.IsVisible=activate.IsVisible=false;
    }
    private void Busy(bool value){busy=value;Buttons();}
    private void Info(JObject result,bool fill=false)
    {
        MaaUmaPacerPage.UpdateDistributionStatus(distribution,result);
        hasActivationReceipt=(string?)result["distribution_source"]=="activation";
        ServiceInfoChanged?.Invoke(result);
        active=(bool?)result["activated"]==true;
        var configured=(bool?)result["configured"]==true;
        if(fill){address.Text=(string?)result["base_url"] ?? "";channel.SelectedIndex=(string?)result["channel"]=="stable" ? 1 : 0;savedAddress=(address.Text ?? "").Trim();savedChannel=channel.SelectedIndex;}
        serviceStatus.Text=active ? "已保存授权，运行前会联网验证。复盘仅在确认提交后上传，仅维护者可见。" : configured ? "已保存服务地址，请在本机激活邀请码后运行。" : "服务未配置。运行前需激活本机；本地复盘导出仍可使用。";
        if(fill && result["last_update"] is JObject last) {
            lastInstall.Text="上次安装："+((string?)last["message"] ?? "结果未确认。");
            if((string?)last["version"] is string version)lastInstall.Text+="（"+version+"）";
            if((string?)last["code"] is string code)lastInstall.Text+=" 错误码："+code;
        }
        Buttons();
    }
    private async Task LoadInfo()
    {
        if(busy)return;Busy(true);
        try {Info(await client.Request(new JObject {["action"]="service_info"}),true);}
        catch(Exception){message.Text="无法读取服务状态，请检查服务配置；本地复盘导出仍可使用。";}
        finally {Busy(false);}
    }
    private async Task Configure()
    {
        if(busy)return;Busy(true);InvalidateDownload();
        try {
            var result=await client.Request(new JObject {["action"]="configure_service",["base_url"]=(address.Text ?? "").Trim(),["channel"]=channel.SelectedIndex==1 ? "stable" : "preview"});
            message.Text=(string?)result["message"] ?? "服务配置未保存。";
            if((bool?)result["ok"]==true){savedAddress=(address.Text ?? "").Trim();savedChannel=channel.SelectedIndex;Info(result);}
        } catch(Exception){message.Text="服务配置未保存，请检查安装包。";}
        finally {Busy(false);}
    }
    private async Task Activate()
    {
        if(busy || ConfigurationDirty)return;Busy(true);
        try {
            var result=await client.Request(new JObject {["action"]="activate",["invite_code"]=(invite.Text ?? "").Trim()});
            message.Text=(string?)result["message"] ?? "邀请码未激活。";
            if((bool?)result["ok"]==true){invite.Text="";Info(await client.Request(new JObject {["action"]="service_info"}));}
        } catch(Exception){message.Text="邀请码未激活，可以稍后重试。";}
        finally {Busy(false);}
    }
    private async Task Update(string action)
    {
        message.Text=MaaUmaPacerPage.UpdateNotice; return;

        if(busy || !active || ConfigurationDirty)return;
        using var cancellation=new CancellationTokenSource();
        if(action=="prepare"){downloadCancellation=cancellation;downloadProgress.IsIndeterminate=true;downloadProgress.Value=0;}
        Busy(true);prepared=null;message.Text=action=="check" ? "正在检查更新……" : "正在准备下载；当前程序不会被替换……";
        try {
            void Progress(JObject value)=>Dispatcher.UIThread.Post(()=>{
                if(downloadCancellation!=cancellation || cancellation.IsCancellationRequested)return;
                var stage=(string?)value["stage"];
                if(stage=="downloading" && (long?)value["total_bytes"] is long total && total>0) {
                    var done=(long?)value["downloaded_bytes"] ?? 0;var rate=(double?)value["bytes_per_second"] ?? 0;
                    downloadProgress.IsIndeterminate=false;downloadProgress.Value=Math.Clamp(100.0*done/total,0,100);
                    message.Text=$"正在下载：{done/1048576.0:F1} / {total/1048576.0:F1} MB（{downloadProgress.Value:F0}%），{rate/1024.0:F0} KB/s";
                } else {downloadProgress.IsIndeterminate=true;message.Text=stage switch {
                    "verifying"=>"下载完成，正在校验更新包……","preparing"=>"校验通过，正在准备安装……",
                    "checking"=>"正在读取更新信息……",_=>"正在连接下载服务器……"};}
            });
            var operation=client.Update(action,action=="prepare" ? Progress : null,cancellation.Token);
            if(action=="prepare")downloadOperation=operation;
            var result=await operation;var state=(string?)result["status"];
            downloadCancellation=null;downloadOperation=null;Buttons();
            message.Text=(string?)result["message"] ?? "更新操作未完成。";
            if((string?)result["version"] is string version && version.Length>0)message.Text+="（"+version+"）";
            if((bool?)result["channel_switch"]==true)message.Text="可切换到所选频道的版本（"+(string?)result["version"]+"），配置与授权会保留。";
            notes.Text=(string?)result["notes"] ?? "";
            available=(bool?)result["ok"]==true && state=="update_available";
            prepared=(bool?)result["ok"]==true && state=="prepared" ? result : null;
            if((bool?)result["ok"]==true && !hasActivationReceipt) {
                // Existing activated clients receive their public source marker
                // on this user-initiated request, without background networking.
                try {
                    var identity=await client.Request(new JObject {["action"]="refresh_distribution"});
                    if((bool?)identity["ok"]==true)Info(identity);
                } catch(Exception) { /* Marker retrieval does not undo a successful update check/download. */ }
            }
        } catch(OperationCanceledException){prepared=null;message.Text="下载已取消，当前程序未替换；可以重新下载。";}
        catch(Exception){available=false;message.Text="更新操作未完成；当前程序和配置仍保留。";}
        finally {downloadCancellation=null;downloadOperation=null;Busy(false);}
    }
    public async Task CancelDownloadAndWait()
    {
        downloadCancellation?.Cancel();
        if(downloadOperation is Task pending)try {await pending;}catch(OperationCanceledException){ }
    }
    private async Task Install()
    {
        message.Text=MaaUmaPacerPage.UpdateNotice; return;

        if(busy || prepared==null || runtimeState is not ("idle" or "stopped"))return;
        Busy(true);
        try {await install(prepared);}
        catch(Exception error){message.Text="尚未安装："+error.Message;}
        finally {Busy(false);}
    }
}
