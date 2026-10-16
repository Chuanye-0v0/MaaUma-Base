using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed class MaaUmaNotificationSettingsView : UserControl
{
    private readonly Func<JObject,Task<JObject>> request;
    private readonly Func<bool> privateEvents;
    private readonly Func<Task>? refreshCapabilities;
    private readonly TextBox token=new(){Name="MaaUmaNotificationToken",PasswordChar='●',Watermark="填写 PushPlus token；保存后隐藏"};
    private readonly TextBlock status=new(){Name="MaaUmaNotificationStatus",TextWrapping=Avalonia.Media.TextWrapping.Wrap};
    private readonly Dictionary<string,CheckBox> events=new();
    private readonly List<Button> buttons=new();
    private bool busy;
    public MaaUmaNotificationSettingsView() : this(MaaUmaNotifications.Request,
        ()=>MaaUmaActivationView.PrivateEventsAvailable&&Directory.Exists(Path.Combine(AppContext.BaseDirectory,"pacer")),
        MaaUmaActivationView.RefreshNotificationCapabilities) { }
    public MaaUmaNotificationSettingsView(Func<JObject,Task<JObject>> request,Func<bool>? privateEvents=null,Func<Task>? refreshCapabilities=null)
    {
        Name="MaaUmaPushPlusSettings";this.request=request;this.privateEvents=privateEvents??(()=>false);this.refreshCapabilities=refreshCapabilities;
        var body=new StackPanel {Spacing=10,Children={
            new TextBlock {Text="PushPlus",FontSize=18},token,
            new TextBlock {Text="全局统一保存；无需选择模拟器。测试只使用已保存的令牌。",TextWrapping=Avalonia.Media.TextWrapping.Wrap}}};
        var actions=new WrapPanel();
        foreach(var row in new[]{("保存令牌","save"),("关闭推送","disable"),("发送测试通知","test"),("刷新状态","status")}) {
            var button=new Button {Content=row.Item1,Name="MaaUmaNotify_"+row.Item2,Margin=new Thickness(0,0,8,6)};
            button.Click+=async(_,_)=>await Execute(row.Item2);actions.Children.Add(button);buttons.Add(button);
        }
        body.Children.Add(actions);body.Children.Add(status);
        body.Children.Add(new TextBlock {Text="事件通知",FontSize=16});
        foreach(var row in new[]{("task_completed","任务完成"),("task_failed","任务失败"),("seed_factor_hit","种马因子命中"),
            ("cut_hit","刷切命中"),("manual_takeover","等待人工接管"),("career_completed","育成完成"),("career_stopped","育成异常停止")}) {
            var check=new CheckBox {Name="MaaUmaNotifyEvent_"+row.Item1,Content=row.Item2,IsChecked=true};events[row.Item1]=check;body.Children.Add(check);
        }
        var saveEvents=new Button {Name="MaaUmaSaveNotificationEvents",Content="保存事件开关"};
        saveEvents.Click+=async(_,_)=>await Execute("set_events");buttons.Add(saveEvents);body.Children.Add(saveEvents);
        Content=body;UpdateCapabilities();
        Loaded+=async(_,_)=>{
            MaaUmaActivationView.CapabilitiesChanged+=UpdateCapabilities;UpdateCapabilities();
            var capabilityRead=RefreshCapabilities();await Execute("status");await capabilityRead;
        };
        Unloaded+=(_,_)=>MaaUmaActivationView.CapabilitiesChanged-=UpdateCapabilities;
    }
    private void UpdateCapabilities()
    {
        var available=privateEvents();foreach(var row in events)
            row.Value.IsVisible=row.Key is "task_completed" or "task_failed" or "seed_factor_hit"||available;
    }
    private async Task RefreshCapabilities()
    {
        try {if(refreshCapabilities!=null)await refreshCapabilities();UpdateCapabilities();}
        catch { /* Public notification settings remain usable without private capability status. */ }
    }
    public async Task Execute(string action)
    {
        if(busy)return;
        if(action=="test"&&!string.IsNullOrWhiteSpace(token.Text)){status.Text="请先保存新令牌，再发送测试通知。";return;}
        var input=new JObject {["action"]=action};
        if(action=="save"){input["token"]=(token.Text??"").Trim();token.Text="";}
        else if(action=="set_events")input["events"]=new JObject(events.Where(row=>row.Value.IsVisible).Select(row=>new JProperty(row.Key,row.Value.IsChecked==true)));
        else if(action is not ("status" or "disable" or "test"))return;
        busy=true;token.IsEnabled=false;foreach(var button in buttons)button.IsEnabled=false;
        try {
            var result=await request(input);
            if(result["events"] is JObject flags)foreach(var row in events)if(flags[row.Key]?.Type==JTokenType.Boolean)row.Value.IsChecked=(bool)flags[row.Key]!;
            status.Text=(string?)result["status"] switch {
                "accepted"=>"测试通知已由服务受理，请查看接收端；尚未确认送达。",
                "disabled"=>"PushPlus 已明确关闭；旧令牌来源不会重新启用。",
                "not_configured"=>"尚未保存可用令牌，请先填写并保存。",
                "saved_token_unreadable"=>"已保存的通知配置无法读取，请重新保存令牌；未回退旧来源。",
                "failed" or "rejected"=>"通知未发送成功；不会自动重试。",
                _=>(bool?)result["ok"]==false?"通知设置未完成，请检查后重试。":
                    (string?)result["mode"]=="off"?"PushPlus 已关闭。":
                    (string?)result["mode"]=="legacy"?"尚未覆盖旧来源查找；保存令牌后统一使用公共配置。":
                    action=="set_events"?"事件开关已保存。":"通知设置已保存或读取，令牌保持隐藏。"};
        }catch {status.Text="通知操作尚未确认；不会自动重复发送或保存。";}
        finally {input.Remove("token");busy=false;token.IsEnabled=true;foreach(var button in buttons)button.IsEnabled=true;}
    }
}
