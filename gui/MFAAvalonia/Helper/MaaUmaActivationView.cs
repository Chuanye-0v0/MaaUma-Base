using System;
using System.Diagnostics;
using System.Collections.Concurrent;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Shared suite activation/update entry. No Pacer control or business dependency.</summary>
public sealed class MaaUmaActivationView : UserControl
{
    public const string UpdateNotice="基础更新无需激活，请使用“系统设置 → 更新 / 反馈”。";
    public const string DefaultServiceUrl="https://maauma-private.2234198378.workers.dev";
    public const string FeedbackUrl="https://github.com/Chuanye-0v0/MaaUma-Base/issues/new/choose";
    public static Func<Task<bool>>? AdditionalIdleCheck { get; set; }
    public static bool PrivateEventsAvailable { get; private set; }
    public static event Action? CapabilitiesChanged;
    private static readonly SemaphoreSlim ActivationFlight = new(1, 1);
    private readonly string root;
    private readonly Func<JObject, Task<JObject>> request;
    private readonly Func<JObject,Action<JObject>,Task<JObject>>? progressRequest;
    private readonly MaaUmaUpdateProgressView downloadProgress=new();
    private readonly ConcurrentQueue<(Guid Invocation,JObject Event)> preparationEvents=new();
    private readonly Func<Task<bool>> idle;
    private readonly Action<ProcessStartInfo> launch;
    private readonly Action exit;
    private readonly Action<ProcessStartInfo> openFeedback;
    private readonly TextBox address = new() { Name="MaaUmaServiceUrl", Text=DefaultServiceUrl, Watermark="维护者预置的 HTTPS 服务地址" };
    private readonly TextBox code = new() { Name="MaaUmaPersonalCode", Watermark="输入测试版激活码", PasswordChar='●' };
    private readonly ComboBox channel = new() { Name="MaaUmaChannel", ItemsSource=new[]{"稳定版","预览版"}, SelectedIndex=1 };
    private readonly Button feedback = new() { Name="MaaUmaFeedback", Content="问题反馈" };
    private readonly Button configure = new() { Name="MaaUmaConfigureService", Content="保存设置" };
    private readonly Button activate = new() { Name="MaaUmaActivate", Content="激活" };
    private readonly Button refresh = new() { Name="MaaUmaRefreshLicense", Content="刷新状态" };
    private readonly Button prepare = new() { Name="MaaUmaPrepareUpdate", Content="检查并准备更新" };
    private readonly Button install = new() { Name="MaaUmaInstallUpdate", Content="安装并重开", IsEnabled=false };
    private readonly TextBlock trace = new() { Name="MaaUmaTraceId", Text="", IsVisible=false, TextWrapping=TextWrapping.Wrap };
    private readonly TextBlock rights = new() { Name="MaaUmaPermissions", Text="", IsVisible=false, TextWrapping=TextWrapping.Wrap };
    private readonly Expander settings = new() { Name="MaaUmaUpdateSettings", Header="更新设置", IsExpanded=false, HorizontalAlignment=HorizontalAlignment.Stretch };
    private readonly Expander activation = new() { Name="MaaUmaTestActivation", Header="测试版激活", IsExpanded=false, HorizontalAlignment=HorizontalAlignment.Stretch };
    private readonly TextBlock message = new() { Name="MaaUmaActivationStatus", TextWrapping=TextWrapping.Wrap };
    private readonly ProgressBar progress = new() { Name="MaaUmaDownloadProgress", IsIndeterminate=true, IsVisible=false, Height=8 };
    private readonly DispatcherTimer timer = new() { Interval=TimeSpan.FromSeconds(2) };
    private bool busy, autoPrepared, pacerAuthorized, pacerInstalled, previewAllowed, currentIdle;
    private string? ticket;
    private string savedChannel="preview",savedAddress=DefaultServiceUrl;

    public MaaUmaActivationView() : this(AppContext.BaseDirectory) { }
    public MaaUmaActivationView(string packageRoot, Func<JObject,Task<JObject>>? service=null,
        Func<Task<bool>>? runtimeIdle=null, Action<ProcessStartInfo>? launchHelper=null, Action? closeWindow=null,
        Action<ProcessStartInfo>? feedbackBrowser=null,Func<JObject,Action<JObject>,Task<JObject>>? progressService=null)
    {
        root=Path.GetFullPath(packageRoot);var client=new SuiteClient(root);
        request=service??client.Request;progressRequest=progressService??(service==null?client.RequestWithProgress:null);
        idle=runtimeIdle ?? RuntimeIdle;
        openFeedback=feedbackBrowser ?? (info=>{using var browser=Process.Start(info);});
        launch=launchHelper ?? (info=>{using var child=Process.Start(info) ?? throw new IOException("无法启动安装助手。");});
        exit=closeWindow ?? (()=>{if(TopLevel.GetTopLevel(this) is Window window)window.Close();});
        var body=new StackPanel {Margin=new Thickness(24),Spacing=12,MaxWidth=920,HorizontalAlignment=HorizontalAlignment.Stretch};
        body.Children.Add(new TextBlock {Name="MaaUmaUpdateTitle",Text="版本更新",FontSize=25,FontWeight=FontWeight.Bold});
        body.Children.Add(new TextBlock {Text="基础更新无需激活。下载完成后，请停止任务再安装。",TextWrapping=TextWrapping.Wrap});
        var actions=new WrapPanel {Orientation=Orientation.Horizontal};
        foreach(var button in new[]{refresh,prepare,install,feedback}){button.Margin=new Thickness(0,0,12,8);actions.Children.Add(button);}
        body.Children.Add(actions);body.Children.Add(progress);body.Children.Add(downloadProgress);body.Children.Add(message);
        var serviceFields=new StackPanel {Spacing=10,Children={
            new TextBlock {Text="服务地址"},address,new TextBlock {Text="更新频道"},channel,configure}};
        var activationFields=new StackPanel {Spacing=10,Children={code,activate,rights,trace}};
        serviceFields.Bind(IsVisibleProperty,settings.GetObservable(Expander.IsExpandedProperty));
        activationFields.Bind(IsVisibleProperty,activation.GetObservable(Expander.IsExpandedProperty));
        settings.Content=serviceFields;activation.Content=activationFields;
        body.Children.Add(settings);body.Children.Add(activation);
        Content=new ScrollViewer {Content=body};
        configure.Click+=async(_,_)=>await Configure();activate.Click+=async(_,_)=>await Activate();
        refresh.Click+=async(_,_)=>await RefreshInfo();prepare.Click+=async(_,_)=>await PrepareUpdate();
        install.Click+=async(_,_)=>await Install();channel.SelectionChanged+=(_,_)=>Buttons();
        feedback.Click+=(_,_)=>{
            try {openFeedback(new ProcessStartInfo(FeedbackUrl){UseShellExecute=true});}
            catch {message.Text="无法打开浏览器，请稍后重试。";}
        };
        address.TextChanged+=(_,_)=>{code.Text="";ticket=null;if(!busy)downloadProgress.Clear();Buttons();};
        Loaded+=async(_,_)=>{timer.Start();try{downloadProgress.RefreshClock();}catch{}await RefreshInfo();};Unloaded+=(_,_)=>timer.Stop();
        timer.Tick+=async(_,_)=>{try{DrainPreparationProgress();downloadProgress.RefreshClock();}catch{}if(!busy)await UpdateIdle();};
    }

    private string SelectedChannel=>channel.SelectedIndex==1 ? "preview" : "stable";
    private void Buttons()
    {
        configure.IsEnabled=refresh.IsEnabled=!busy;
        activate.IsEnabled=!busy && (address.Text??"").Trim()==savedAddress;
        prepare.IsEnabled=activate.IsEnabled && SelectedChannel==savedChannel;
        code.IsEnabled=address.IsEnabled=channel.IsEnabled=!busy;
        install.IsEnabled=!busy && ticket!=null && currentIdle && SelectedChannel==savedChannel && (address.Text??"").Trim()==savedAddress;
        progress.IsVisible=busy&&!downloadProgress.IsActive;
        prepare.Content=downloadProgress.HasFailed?"重新尝试下载":"检查并准备更新";
    }
    private void Busy(bool value){busy=value;Buttons();}
    private void Apply(JObject result,bool displayMessage=true)
    {
        if(result.Property("trace_id")!=null) {
            var id=(string?)result["trace_id"];
            trace.Text=id!=null && System.Text.RegularExpressions.Regex.IsMatch(id,@"\AMU-(?:[0-9A-F]{4}-){3}[0-9A-F]{4}\z") ? "编号："+id : "";
        }
        if(result.Property("products")!=null) {
            var products=result["products"] is JArray array ? array.Values<string>().Where(x=>x!=null).ToArray()
                : result["products"] is JObject map ? map.Properties().Where(x=>x.Value.Type==JTokenType.Boolean && (bool)x.Value || x.Value.Type==JTokenType.Object).Select(x=>x.Name).ToArray() : Array.Empty<string>();
            pacerAuthorized=products.Contains("uma-cut");
        }
        if(result.Property("allow_preview")!=null)previewAllowed=(bool?)result["allow_preview"]==true;
        var modules=result["installed_modules"] ?? result["modules"]?["components"];
        if(modules!=null)pacerInstalled=modules is JArray installed && installed.Values<string>().Any(x=>x is "pacer" or "uma-cut")
            || modules is JObject installedMap && installedMap["pacer"] is JToken entry && entry.Type is not (JTokenType.Null or JTokenType.Boolean)
            || modules is JObject booleanMap && booleanMap["pacer"]?.Type==JTokenType.Boolean && (bool)booleanMap["pacer"]!;
        var available=pacerAuthorized&&pacerInstalled;
        if(PrivateEventsAvailable!=available){PrivateEventsAvailable=available;CapabilitiesChanged?.Invoke();}
        rights.Text=pacerAuthorized ? "测试版状态：已激活" : pacerInstalled ? "测试版状态：待确认" : "";
        rights.IsVisible=pacerAuthorized || pacerInstalled;
        trace.IsVisible=rights.IsVisible && !string.IsNullOrEmpty(trace.Text);
        var canPreview=previewAllowed || !pacerAuthorized && !pacerInstalled;
        var selected=(string?)result["channel"] ?? savedChannel;
        channel.ItemsSource=canPreview ? new[]{"稳定版","预览版"} : new[]{"稳定版"};
        channel.SelectedIndex=canPreview && selected=="preview" ? 1 : 0;savedChannel=selected;
        if((string?)result["base_url"] is string url){address.Text=url;savedAddress=url;}
        var prepared=result["component"] as JObject;
        if(result.Property("ticket")!=null)ticket=result["ticket"]?.Type==JTokenType.String ? (string?)result["ticket"] : null;
        else if((bool?)prepared?["ok"]==true && prepared?["ticket"]?.Type==JTokenType.String)ticket=(string?)prepared["ticket"];
        if(displayMessage)message.Text=(bool?)result["ok"]==false
            ? "操作未完成，请检查更新设置后重试。当前安装未修改。"
            : (string?)result["status"] switch {
                "prepared"=>"更新已准备；停止任务后可安装并重开。",
                "current" or "no_update"=>"当前已是可用的最新版本。",
                "configured"=>"更新设置已保存。",
                "service_info"=>"版本信息已刷新。",
                _=>"状态已更新。"
            };
        if(displayMessage && result["pacer_warning"] is JObject)
            message.Text+=" 部分内容准备未完成，可稍后重试。";
        Buttons();
    }
    private async Task UpdateIdle(){try{currentIdle=await idle();}catch{currentIdle=false;}Buttons();}
    private static async Task<bool> RuntimeIdle()
    {
        try {
            if(Instances.InstanceTabBarViewModel.Tabs.Any(x=>x.IsRunning || x.TaskQueueViewModel?.IsRunning==true))return false;
            return AdditionalIdleCheck==null || await AdditionalIdleCheck();
        }catch{return false;}
    }
    private static readonly SemaphoreSlim NotificationCapabilityFlight=new(1,1);
    public static async Task RefreshNotificationCapabilities()
    {
        if(!Directory.Exists(Path.Combine(AppContext.BaseDirectory,"pacer")))return;
        if(!await NotificationCapabilityFlight.WaitAsync(0))return;
        try {
            // Capability inspection only; unlike RefreshInfo this cannot prepare
            // or download a component while the notification page is opened.
            var result=await new SuiteClient(AppContext.BaseDirectory).Request(new JObject {["action"]="info"});
            var products=result["products"];
            var authorized=products is JArray list&&list.Values<string>().Contains("uma-cut")
                ||products is JObject map&&map["uma-cut"] is JToken right&&right.Type is not (JTokenType.Null or JTokenType.Boolean)
                ||products is JObject flags&&flags["uma-cut"]?.Type==JTokenType.Boolean&&(bool)flags["uma-cut"]!;
            var modules=result["installed_modules"]??result["modules"]?["components"];
            var installed=modules is JArray entries&&entries.Values<string>().Any(x=>x is "pacer" or "uma-cut")
                ||modules is JObject installedMap&&installedMap["pacer"] is JToken entry&&entry.Type is not (JTokenType.Null or JTokenType.Boolean)
                ||modules is JObject booleanMap&&booleanMap["pacer"]?.Type==JTokenType.Boolean&&(bool)booleanMap["pacer"]!;
            PrivateEventsAvailable=(bool?)result["ok"]!=false&&authorized&&installed;
            CapabilitiesChanged?.Invoke();
        }catch {PrivateEventsAvailable=false;CapabilitiesChanged?.Invoke();}
        finally {NotificationCapabilityFlight.Release();}
    }

    public async Task RefreshInfo()
    {
        if(busy)return;Busy(true);
        try {
            var result=await request(new JObject {["action"]="info"});Apply(result);
            if((bool?)result["ok"]==true && SelectedChannel==savedChannel && pacerAuthorized && !pacerInstalled && ticket==null && !autoPrepared){
                autoPrepared=true;Apply(await RequestPreparation(new JObject {["action"]="prepare_pacer"}));
            }
        }catch{message.Text="版本信息读取未完成，可以重试；当前程序与任务未修改。";}
        finally{Busy(false);await UpdateIdle();}
    }
    public async Task Configure()
    {
        if(busy)return;Busy(true);ticket=null;
        try {
            var result=await request(new JObject {["action"]="configure",["base_url"]=(address.Text??"").Trim(),["channel"]=SelectedChannel});
            Apply(result);autoPrepared=false;
            if((bool?)result["ok"]==true)Apply(await request(new JObject {["action"]="info"}));
        }
        catch{message.Text="设置保存未确认，请检查后重试。";}finally{Busy(false);await UpdateIdle();}
    }
    public async Task Activate()
    {
        if(busy || (address.Text??"").Trim()!=savedAddress || string.IsNullOrWhiteSpace(code.Text) || !ActivationFlight.Wait(0))return;
        Busy(true);autoPrepared=true;ticket=null;
        var input=new JObject {["action"]="activate",["invite_code"]=(code.Text??"").Trim()};code.Text="";
        message.Text="正在确认激活并准备内容，请稍候。";
        try {
            var result=await RequestPreparation(input);Apply(result,displayMessage:false);
            // Never display an activation response that could echo a personal code.
            message.Text=(bool?)result["ok"]==true ? ticket!=null ? "激活已完成，内容已准备；停止任务后可安装并重开。"
                : (bool?)result["component"]?["ok"]==false ? "激活已完成，下载未完成；可用“检查并准备更新”重试。" : "激活已完成，状态已更新。"
                : "激活未完成，请检查激活码或更新设置后重试。";
        }catch{message.Text="激活结果未确认，请先刷新状态；不会自动重复提交激活码。";}
        finally{input.Remove("invite_code");ActivationFlight.Release();Busy(false);await UpdateIdle();}
    }
    public async Task PrepareUpdate()
    {
        if(busy || SelectedChannel!=savedChannel || (address.Text??"").Trim()!=savedAddress)return;Busy(true);ticket=null;
        message.Text="正在准备更新，可切换到其它页面。";
        try {Apply(await RequestPreparation(new JObject {["action"]="prepare_update"}));}
        catch{message.Text="更新准备未确认，可以重试；当前安装未被替换。";}finally{Busy(false);await UpdateIdle();}
    }
    private void DrainPreparationProgress()
    {
        while(preparationEvents.TryDequeue(out var item)) {
            try {downloadProgress.Accept(item.Invocation,item.Event);}catch { }
        }
        try {Buttons();}catch { }
    }
    private async Task<JObject> RequestPreparation(JObject input)
    {
        var invocation=Guid.NewGuid();
        try {downloadProgress.Begin(invocation);Buttons();}catch { }
        void Receive(JObject value) {
            try {preparationEvents.Enqueue((invocation,(JObject)value.DeepClone()));
                Dispatcher.UIThread.Post(DrainPreparationProgress);}catch { }
        }
        try {
            var result=progressRequest==null?await request(input):await progressRequest(input,Receive);
            try {DrainPreparationProgress();downloadProgress.End(invocation,result);Buttons();}catch { }
            return result;
        }catch {try {DrainPreparationProgress();downloadProgress.Fail(invocation);Buttons();}catch { }throw;}
    }

    private string OwnedPath(JObject result,string name)
    {
        var text=(string?)result[name];if(string.IsNullOrWhiteSpace(text))throw new IOException();
        var path=Path.GetFullPath(text);var prefix=root.TrimEnd(Path.DirectorySeparatorChar,Path.AltDirectorySeparatorChar)+Path.DirectorySeparatorChar;
        if(!path.StartsWith(prefix,StringComparison.OrdinalIgnoreCase) || !File.Exists(path))throw new IOException();
        return path;
    }
    public async Task Install()
    {
        if(busy || ticket==null || SelectedChannel!=savedChannel || (address.Text??"").Trim()!=savedAddress)return;Busy(true);
        try {
            if(!await idle()){message.Text="仍有任务运行或状态未确认，请全部停止后安装。";return;}
            var result=await request(new JObject {["action"]="install",["ticket"]=ticket,["parent_pid"]=Environment.ProcessId});
            if((bool?)result["ok"]!=true){message.Text="安装准备未确认，当前程序仍保留。";return;}
            if(!await idle()){message.Text="任务状态发生变化，尚未启动安装助手；请停止全部任务后重试。";return;}
            if(result["helper_arguments"] is not JArray arguments || arguments.Any(x=>x.Type!=JTokenType.String))throw new IOException();
            var executable=OwnedPath(result,"helper_executable");
            var script=(string?)result["helper_script"] ?? arguments.Values<string>().FirstOrDefault(x=>x?.EndsWith(".py",StringComparison.OrdinalIgnoreCase)==true);
            _=OwnedPath(new JObject {["script"]=script},"script");
            var working=root;
            if((string?)result["helper_working_directory"] is string directory) {
                working=Path.GetFullPath(directory);
                if(!working.StartsWith(root.TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase) || !Directory.Exists(working))throw new IOException();
            }
            var info=new ProcessStartInfo(executable){WorkingDirectory=working,UseShellExecute=false,CreateNoWindow=true};
            foreach(var argument in arguments.Values<string>())info.ArgumentList.Add(argument!);
            info.Environment["DOTNET_ROOT"]=Path.Combine(root,"dotnet");
            launch(info);if(TopLevel.GetTopLevel(this) is Window window)window.IsEnabled=false;exit();
        }catch{message.Text="独立安装助手尚未确认，当前程序没有退出或替换。";}
        finally{Busy(false);}
    }

    private sealed class SuiteClient(string root)
    {
        private readonly SemaphoreSlim gate=new(1,1);
        public Task<JObject> Request(JObject input)=>RequestCore(input,null);
        public Task<JObject> RequestWithProgress(JObject input,Action<JObject> progress)=>
            RequestCore(input,(string?)input["action"] is "activate" or "prepare_update" or "prepare_pacer"?progress:null);
        private async Task<JObject> RequestCore(JObject input,Action<JObject>? progress)
        {
            await gate.WaitAsync();
            try {
                var python=Path.Combine(root,"runtime","python.exe");var script=Path.Combine(root,"backend","scripts","maauma_service.py");
                var info=new ProcessStartInfo(python){WorkingDirectory=root,UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardInputEncoding=new UTF8Encoding(false),StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8};
                foreach(var arg in new[]{"-B","-X","utf8",script,"--root",root})info.ArgumentList.Add(arg);
                if(progress!=null)info.ArgumentList.Add("--progress-jsonl");
                info.Environment["DOTNET_ROOT"]=Path.Combine(root,"dotnet");
                using var process=Process.Start(info) ?? throw new IOException();
                using var timeout=new CancellationTokenSource(TimeSpan.FromHours(2));
                var response=MaaUmaUpdateProgressPipe.ReadResponseAsync(process.StandardOutput,process.StandardError,progress,timeout.Token);
                await process.StandardInput.WriteAsync(input.ToString(Formatting.None));process.StandardInput.Close();
                input.Remove("invite_code");
                try{await process.WaitForExitAsync(timeout.Token);}catch(OperationCanceledException){try{process.Kill(true);}catch{}throw new IOException();}
                return await response;
            }finally{gate.Release();}
        }
    }
}
