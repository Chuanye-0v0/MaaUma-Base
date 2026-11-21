using System;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Local display refresh; only the explicit test button requests the AI provider.</summary>
public sealed class MaaUmaAiStatusPanel : StackPanel
{
    private readonly TextBlock selected = Text("编辑中的配置：本地策略");
    private readonly TextBlock connection = Text("AI 接口状态尚未读取。");
    private readonly TextBlock model = Text("");
    private readonly TextBlock current = Text("尚未启动当前局；保存接口不会自动启用 AI。");
    private readonly TextBlock duration = Text("");
    private readonly Button test = new() { Name="PacerAiTestConnection", Content="测试连接" };
    private readonly Button refresh = new() { Name="PacerAiRefreshUsage", Content="刷新状态" };
    private readonly StackPanel explanation = new() { Spacing=6 };
    private readonly Expander detail;
    private bool busy;
    public Func<JObject,Task<JObject>>? RequestAsync { get; set; }

    public MaaUmaAiStatusPanel()
    {
        Name="PacerAiStatusPanel"; Spacing=8;
        selected.Name="PacerAiSelectedMode"; connection.Name="PacerAiConnectionStatus";
        current.Name="PacerAiRuntimeStatus"; duration.Name="PacerAiResponseDuration";
        Children.Add(selected); Children.Add(connection); Children.Add(model);
        var buttons=new WrapPanel { Orientation=Orientation.Horizontal };
        test.Margin=refresh.Margin=new Thickness(0,0,8,4);
        buttons.Children.Add(test); buttons.Children.Add(refresh); Children.Add(buttons);
        Children.Add(Text("测试连接会发送一次小型 API 请求，可能产生少量用量。保存接口、打开页面和刷新状态只读取本机信息。"));
        Children.Add(current); Children.Add(duration);
        detail=new Expander { Name="PacerAiDecisionExplanation", Header="决策说明", Content=explanation, IsVisible=false };
        Children.Add(detail);
        test.Click+=async(_,_)=>await TestAsync();
        refresh.Click+=async(_,_)=>await RefreshAsync();
    }

    private static TextBlock Text(string value)=>new() { Text=value, TextWrapping=TextWrapping.Wrap };
    private static string S(JToken? token,int max=500)
    {
        if(token?.Type!=JTokenType.String)return "";
        var value=token.Value<string>()??"";
        return value.Length<=max?value:value[..max];
    }
    private static string Mode(string? mode)=>mode switch { "critical"=>"关键时刻 AI", "full"=>"每回合 AI", "local"=>"本地策略", _=>"尚未启动" };
    private static string Number(JToken? token)
    {
        if(token?.Type is not (JTokenType.Integer or JTokenType.Float))return "—";
        var value=token.Value<double>();
        return double.IsFinite(value)?value.ToString("0.###",CultureInfo.InvariantCulture):"—";
    }
    public void SetSelectedMode(string? mode)
        =>selected.Text="编辑中的配置："+Mode(mode)+"；保存后用于新一轮，本局继续沿用启动时设置。";

    public void Render(JObject? value)
    {
        var provider=value?["connection"] as JObject;
        var checkedValue=provider?["test"] as JObject;
        connection.Text=(bool?)provider?["configured"]!=true ? "AI 接口未配置或无法读取。" : S(checkedValue?["status"]) switch {
            "success"=>"连接测试成功；已验证当前地址、模型与密钥。",
            "failed"=>"连接测试失败："+S(checkedValue?["message"]),
            _=>"AI 接口已保存，当前配置尚未测试连接。" };
        model.Text=(bool?)provider?["configured"]==true ? "模型："+S(provider["model"],200)+"　接口："+S(provider["base_url"],250) : "";
        var runtime=value?["runtime"] as JObject;
        current.Text="本局模式："+Mode(S(runtime?["mode"]))+"　"+S(runtime?["message"]);
        if((bool?)runtime?["active"]!=true && S(runtime?["mode"]).Length>0)
            current.Text="当前没有运行中的育成。"+current.Text;
        var elapsed=Number(runtime?["elapsed_seconds"]);
        duration.Text=elapsed is "—" or "0" ? "" : "最近一次 AI 响应等待："+elapsed+" 秒";
        if(S(checkedValue?["status"]) is "success" or "failed")
            duration.Text+=(duration.Text.Length>0?"；":"")+"连接测试耗时："+Number(checkedValue?["elapsed_seconds"])+" 秒";
        explanation.Children.Clear();
        var decision=value?["decision"] as JObject;
        detail.IsVisible=decision!=null;
        if(decision==null)return;
        explanation.Children.Add(Text("决策节点："+S(decision["node"],160)));
        explanation.Children.Add(Text("采用结果："+S(decision["reason"])));
        explanation.Children.Add(Text("最终动作："+(S(decision["action_id"],160) is {Length:>0} action?action:"尚未选择")));
        if((bool?)decision["cached"]==true)explanation.Children.Add(Text("此处复用本回合已有选择，没有重复请求 API。"));
        if(decision["state"] is JArray state && state.Count>0)
            explanation.Children.Add(Text("用到的状态："+string.Join("；",state.OfType<JObject>().Take(16).Select(row=>S(row["label"],80)+"="+Number(row["value"])))));
        if(decision["candidates"] is JArray candidates && candidates.Count>0) {
            explanation.Children.Add(Text("候选对比："));
            foreach(var row in candidates.OfType<JObject>().Take(24)) {
                var flags=((bool?)row["selected"]==true?" [最终选择]":"")+((bool?)row["local"]==true?" [本地推荐]":"")+((bool?)row["allowed"]!=true?" [不参与执行]":"");
                explanation.Children.Add(Text("• "+S(row["action_id"],160)+"　得分 "+Number(row["score"])+flags));
                if(row["components"] is JObject parts) {
                    var items=parts.Properties().Take(12).Where(part=>part.Value.Type is JTokenType.Integer or JTokenType.Float)
                        .Select(part=>part.Name+"="+Number(part.Value));
                    var text=string.Join("；",items);
                    if(text.Length>0)explanation.Children.Add(Text("  "+text));
                }
            }
        }
        if(decision["key_factors"] is JArray factors)
            foreach(var factor in factors.Take(8))explanation.Children.Add(Text("依据："+S(factor)));
        var brief=S(decision["explanation"],300);
        if(brief.Length>0)explanation.Children.Add(Text("模型简要依据："+brief));
    }

    public async Task RefreshAsync()
    {
        if(busy || RequestAsync==null)return;
        busy=true; refresh.IsEnabled=test.IsEnabled=false;
        try {Render((await RequestAsync(new JObject {["action"]="ai_status"}))["ai_status"] as JObject);}
        catch {connection.Text="本机 AI 状态暂未读取，请稍后重试。";}
        finally {busy=false; refresh.IsEnabled=test.IsEnabled=true;}
    }
    public async Task TestAsync()
    {
        if(busy || RequestAsync==null)return;
        busy=true; refresh.IsEnabled=test.IsEnabled=false;
        connection.Text="正在测试已保存的接口、模型和密钥，请稍候……";
        try {Render((await RequestAsync(new JObject {["action"]="ai_test"}))["ai_status"] as JObject);}
        catch {connection.Text="连接测试未完成，请检查已保存配置和网络后重试。";}
        finally {busy=false; refresh.IsEnabled=test.IsEnabled=true;}
    }
}
