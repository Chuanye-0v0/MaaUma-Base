using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Non-secret run preferences; credentials use a separate global settings action.</summary>
public sealed class UmaAiOptions:StackPanel
{
    private readonly ComboBox mode=new() {Name="PacerAiMode",ItemsSource=new[]{"本地策略","关键时刻 AI","每回合 AI"},SelectedIndex=0,MinWidth=200};
    private readonly NumericUpDown timeout=new() {Name="PacerAiTimeout",Minimum=1,Maximum=120,Increment=1,Value=20,FormatString="0"};
    private readonly NumericUpDown gap=new() {Name="PacerAiCloseGap",Minimum=0,Maximum=1000,Increment=1,Value=5,FormatString="0.##"};
    private readonly ComboBox fallback=new() {Name="PacerAiFallback",ItemsSource=new[]{"沿用本地策略","暂停并保留当前局"},SelectedIndex=0};
    private readonly TextBlock description=new() {Name="PacerAiDescription",TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock provider=new() {Name="PacerAiProviderStatus",Text="现有接口状态尚未读取。",TextWrapping=TextWrapping.Wrap};
    private readonly Button refresh=new() {Name="PacerAiRefreshStatus",Content="刷新接口状态"};
    private readonly TextBox baseUrl=new() {Name="PacerAiBaseUrl",Watermark="https://接口地址/v1"};
    private readonly TextBox model=new() {Name="PacerAiModel",Watermark="服务商提供的模型名称"};
    private readonly TextBox key=new() {Name="PacerAiKey",Watermark="填写 API Key；保存成功后清空",PasswordChar='●'};
    private readonly Button saveConnection=new() {Name="PacerAiSaveConnection",Content="保存接口"};
    private readonly Button clearConnection=new() {Name="PacerAiClearConnection",Content="清除接口"};
    private readonly StackPanel advanced=new() {Spacing=8};
    private bool loading;
    public event Action? Changed;
    public Func<Task<JObject>>? RefreshStatus {get;set;}
    public Func<JObject,Task<JObject>>? ConfigureConnection {get;set;}

    public UmaAiOptions()
    {
        Name="PacerAiOptions";Spacing=10;
        Children.Add(Labelled("决策方式",mode));Children.Add(description);Children.Add(provider);
        var connection=new StackPanel {Spacing=8};connection.Children.Add(Labelled("接口地址（OpenAI 兼容）",baseUrl));
        connection.Children.Add(Labelled("模型名称",model));connection.Children.Add(Labelled("API Key",key));
        var buttons=new WrapPanel {Orientation=Orientation.Horizontal};
        foreach(var button in new[]{saveConnection,clearConnection,refresh}){button.Margin=new Avalonia.Thickness(0,0,8,6);buttons.Children.Add(button);}connection.Children.Add(buttons);
        connection.Children.Add(new TextBlock {Text="接口为本机全局配置，三组配置共用。原地址和模型未变时，密钥留空表示保留；更换地址或模型需重新填写。保存只检查格式，不发起付费请求。",TextWrapping=TextWrapping.Wrap,FontSize=12});
        Children.Add(new Expander {Header="AI 接口设置（本机共用）",Content=connection,IsExpanded=true});
        advanced.Children.Add(Labelled("AI 最长等待（秒）",timeout));
        advanced.Children.Add(Labelled("候选分差不超过多少时，请 AI 比较",gap));
        advanced.Children.Add(Labelled("AI 超时或暂不可用时",fallback));Children.Add(advanced);
        Children.Add(new TextBlock {Text="模式随配置1～3分别保存，新一轮生效；暂停后继续沿用本局设置。只发送当前局的结构化状态和候选，不发送截图、账号、ADB 或本地文件路径。密钥仅保存在本机，不写入权重、育成记录或复盘。",TextWrapping=TextWrapping.Wrap,FontSize=12});
        mode.SelectionChanged+=(_,_)=>Updated();fallback.SelectionChanged+=(_,_)=>Updated();
        timeout.ValueChanged+=(_,_)=>Updated();gap.ValueChanged+=(_,_)=>Updated();
        refresh.Click+=async(_,_)=> {
            if(RefreshStatus==null)return;refresh.IsEnabled=false;
            try {SetProvider((await RefreshStatus())["provider"] as JObject);}
            catch {provider.Text="现有接口状态暂未读取，请稍后重试。";}
            finally {refresh.IsEnabled=true;}
        };
        saveConnection.Click+=async(_,_)=>await SaveConnection(false);
        clearConnection.Click+=async(_,_)=>await SaveConnection(true);
        Updated();
    }
    private static StackPanel Labelled(string text,Control value)
    {
        var panel=new StackPanel {Spacing=4};panel.Children.Add(new TextBlock {Text=text,TextWrapping=TextWrapping.Wrap});panel.Children.Add(value);return panel;
    }
    private void Updated()
    {
        description.Text=mode.SelectedIndex switch {
            1=>"平时沿用本地策略；在已启用的关键节点，或候选得分接近时交给 AI 比较。",
            2=>"每次进入可决策回合时请求 AI；页面识别、点击与流程推进仍由现有执行器处理。",
            _=>"保持当前本地策略，不因这项设置请求 AI。"};
        advanced.IsEnabled=mode.SelectedIndex!=0;
        if(!loading)Changed?.Invoke();
    }
    public void SetProvider(JObject? status)
    {
        provider.Text=(bool?)status?["configured"]==true ? "现有 AI 接口已设置（密钥不显示）。" : "现有 AI 接口尚未设置或暂不可读取。";
        baseUrl.Text=(string?)status?["base_url"]??"";model.Text=(string?)status?["model"]??"";
    }
    private async Task SaveConnection(bool clear)
    {
        if(ConfigureConnection==null)return;
        saveConnection.IsEnabled=clearConnection.IsEnabled=refresh.IsEnabled=baseUrl.IsEnabled=model.IsEnabled=key.IsEnabled=false;
        try {
            var request=clear ? new JObject {["action"]="ai_clear"} : new JObject {["action"]="ai_configure",
                ["base_url"]=(baseUrl.Text??"").Trim(),["model"]=(model.Text??"").Trim(),
                ["api_key"]=string.IsNullOrWhiteSpace(key.Text)?null:key.Text.Trim()};
            var status=(await ConfigureConnection(request))["provider"] as JObject;
            SetProvider(status);
            if(clear || (bool?)status?["configured"]==true) {
                key.Text="";
                provider.Text=clear ? "AI 接口已清除；本地策略仍可使用。" : "AI 接口已保存，密钥已隐藏；未发送 API 请求。";
            }
        } catch(Exception error) {provider.Text="接口未保存："+error.Message;}
        finally {saveConnection.IsEnabled=clearConnection.IsEnabled=refresh.IsEnabled=baseUrl.IsEnabled=model.IsEnabled=key.IsEnabled=true;}
    }
    public void Load(JObject? value)
    {
        loading=true;
        try {
            mode.SelectedIndex=(string?)value?["mode"] switch {"critical"=>1,"full"=>2,_=>0};
            timeout.Value=Math.Clamp((decimal?)value?["timeout_seconds"]??20,1,120);
            gap.Value=Math.Clamp((decimal?)value?["close_score_gap"]??5,0,1000);
            fallback.SelectedIndex=(string?)value?["fallback"]=="pause"?1:0;Updated();
        } finally {loading=false;}
    }
    public JObject Read()=>new() {["mode"]=mode.SelectedIndex switch {1=>"critical",2=>"full",_=>"local"},
        ["timeout_seconds"]=(int)(timeout.Value??20),["close_score_gap"]=(double)(gap.Value??5),["fallback"]=fallback.SelectedIndex==1?"pause":"local"};
}
