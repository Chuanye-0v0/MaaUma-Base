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
    private readonly ComboBox downloadSource = new() { Name="MaaUmaDownloadSource", ItemsSource=new[]{"自动（国内优先）","国内下载（腾讯云）","原下载通道"}, SelectedIndex=0 };
    private readonly Button networkCheck = new() { Name="MaaUmaCheckNetwork", Content="检查网络" };
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
    private readonly TextBlock cacheCleanupStatus = new() { Name="MaaUmaCacheCleanupStatus", TextWrapping=TextWrapping.Wrap, FontSize=13, IsVisible=false };
    private readonly TextBlock pacerCleanupStatus = new() { Name="MaaUmaPacerCleanupStatus", TextWrapping=TextWrapping.Wrap, FontSize=13, IsVisible=false };
    private readonly TextBlock startupMaintenanceStatus = new() { Name="MaaUmaStartupMaintenanceStatus", TextWrapping=TextWrapping.Wrap, FontSize=13, IsVisible=false };
    private Task? startupMaintenanceTask;
    private readonly ProgressBar progress = new() { Name="MaaUmaDownloadProgress", IsIndeterminate=true, IsVisible=false, Height=8 };
    private readonly DispatcherTimer timer = new() { Interval=TimeSpan.FromSeconds(2) };
    private bool busy, autoPrepared, pacerAuthorized, pacerInstalled, previewAllowed, currentIdle;
    private string? ticket;
    private string savedChannel="preview",savedAddress=DefaultServiceUrl,savedDownloadSource="auto";

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
        // MaaUma local update announcement v1
        var announcement = new Button { Name="MaaUmaShowUpdateAnnouncement", Content="更新公告" };
        announcement.Click += (_, _) => MaaUmaUpdateAnnouncement.ShowCurrent(TopLevel.GetTopLevel(this) as Window, root);
        foreach(var button in new[]{refresh,prepare,install,feedback,announcement,networkCheck}){button.Margin=new Thickness(0,0,12,8);actions.Children.Add(button);}
        body.Children.Add(actions);body.Children.Add(progress);body.Children.Add(downloadProgress);body.Children.Add(message);body.Children.Add(startupMaintenanceStatus);body.Children.Add(cacheCleanupStatus);body.Children.Add(pacerCleanupStatus);
        var serviceFields=new StackPanel {Spacing=10,Children={
            new TextBlock {Text="服务地址"},address,new TextBlock {Text="更新频道"},channel,
            new TextBlock {Text="下载渠道"},downloadSource,
            new TextBlock {Text="选择后请保存设置，再检查更新。下载渠道不影响激活状态。",TextWrapping=TextWrapping.Wrap},configure}};
        var activationFields=new StackPanel {Spacing=10,Children={code,activate,rights,trace}};
        serviceFields.Bind(IsVisibleProperty,settings.GetObservable(Expander.IsExpandedProperty));
        activationFields.Bind(IsVisibleProperty,activation.GetObservable(Expander.IsExpandedProperty));
        settings.Content=serviceFields;activation.Content=activationFields;
        body.Children.Add(settings);body.Children.Add(activation);
        Content=new ScrollViewer {Content=body};
        configure.Click+=async(_,_)=>await Configure();activate.Click+=async(_,_)=>await Activate();
        refresh.Click+=async(_,_)=>await RefreshInfo();prepare.Click+=async(_,_)=>await PrepareUpdate();
        networkCheck.Click+=async(_,_)=>await CheckConnectivity();
        install.Click+=async(_,_)=>await Install();channel.SelectionChanged+=(_,_)=>Buttons();
        downloadSource.SelectionChanged+=(_,_)=>Buttons();
        feedback.Click+=(_,_)=>{
            try {openFeedback(new ProcessStartInfo(FeedbackUrl){UseShellExecute=true});}
            catch {message.Text="无法打开浏览器，请稍后重试。";}
        };
        address.TextChanged+=(_,_)=>{code.Text="";ticket=null;if(!busy)downloadProgress.Clear();Buttons();};
        Loaded+=async(_,_)=>{timer.Start();try{downloadProgress.RefreshClock();}catch{}await RefreshInfo();};Unloaded+=(_,_)=>timer.Stop();
        timer.Tick+=async(_,_)=>{try{DrainPreparationProgress();downloadProgress.RefreshClock();}catch{}if(!busy)await UpdateIdle();};
    }

    private string SelectedChannel=>channel.SelectedIndex==1 ? "preview" : "stable";
    private string SelectedDownloadSource=>downloadSource.SelectedIndex switch {1=>"cos",2=>"origin",_=>"auto"};
    private bool SettingsSaved=>SelectedChannel==savedChannel && SelectedDownloadSource==savedDownloadSource && (address.Text??"").Trim()==savedAddress;
    private void Buttons()
    {
        configure.IsEnabled=refresh.IsEnabled=!busy;networkCheck.IsEnabled=!busy&&SettingsSaved;
        activate.IsEnabled=!busy && SettingsSaved;
        prepare.IsEnabled=activate.IsEnabled && SelectedChannel==savedChannel;
        code.IsEnabled=address.IsEnabled=channel.IsEnabled=downloadSource.IsEnabled=!busy;
        install.IsEnabled=!busy && ticket!=null && currentIdle && SettingsSaved;
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
        if((string?)result["download_source"] is string source && source is "auto" or "cos" or "origin") {
            savedDownloadSource=source;downloadSource.SelectedIndex=source switch {"cos"=>1,"origin"=>2,_=>0};
        }
        var prepared=result["component"] as JObject;
        if(result.Property("ticket")!=null)ticket=result["ticket"]?.Type==JTokenType.String ? (string?)result["ticket"] : null;
        else if((bool?)prepared?["ok"]==true && prepared?["ticket"]?.Type==JTokenType.String)ticket=(string?)prepared["ticket"];
        if(displayMessage)message.Text=(bool?)result["ok"]==false
            ? MaaUmaUpdateConnectivity.FailureText(result) is { Length:>0 } failure?failure:"操作未完成，请检查更新设置后重试。当前安装未修改。"
            : (string?)result["status"] switch {
                "prepared"=>"更新已准备；停止任务后可安装并重开。",
                "current" or "no_update"=>"当前已是可用的最新版本。",
                "configured"=>"更新设置已保存。",
                "service_info"=>"版本信息已刷新。",
                _=>"状态已更新。"
            };
        if(displayMessage && result["pacer_warning"] is JObject)
            message.Text+=" 部分内容准备未完成，可稍后重试。";
        ApplyMaintenanceResult(result,(string?)result["status"]=="service_info");
        Buttons();
    }
    private static long? CleanupCount(JToken? value)
    {
        if(value?.Type!=JTokenType.Integer)return null;
        var text=value.ToString(Formatting.None);
        return text.Length<=19 && long.TryParse(text,System.Globalization.NumberStyles.None,
            System.Globalization.CultureInfo.InvariantCulture,out var count) && count>=0 ? count : null;
    }
    private static string? CleanupCode(JToken? value)=>value?.Type==JTokenType.String ? (string?)value : null;
    private static string CleanupProtectedReason(JToken? value)=>CleanupCode(value) switch {
        "current_rollback"=>"当前回退备份",
        "live_helper" or "active_download_owner"=>"仍在使用的更新或下载",
        "unfinished_or_failed_transaction"=>"未完成或失败的更新",
        "current_or_future_prepared_update"=>"当前或待安装更新",
        "uncommitted_program_backup"=>"尚未提交的程序备份",
        "unrecognized_stage_content" or "unrecognized_download_content"=>"内容未识别",
        "unreadable_stage_metadata" or "linked_or_unreadable_stage"=>"包含链接或无法读取的暂存",
        "no_installed_history_guard"=>"安装历史尚未确认",
        "download_owner_unidentified" or "unidentified_or_current_download" or "invalid_download_marker"=>"版本或下载归属尚未确认",
        "owner_query_unavailable"=>"下载进程状态暂时无法确认",
        "remove_failed"=>"未能删除的暂存",
        _=>"原因尚未确认的暂存"
    };
    private static string? CleanupDiagnosticToken(JToken? value)
    {
        var token=CleanupCode(value);
        return token is {Length:>0 and <=80} && System.Text.RegularExpressions.Regex.IsMatch(token,@"\A[A-Za-z_][A-Za-z0-9_]*\z") ? token : null;
    }
    private static string? CleanupDiagnosticPhase(JToken? value)
    {
        var phase=CleanupDiagnosticToken(value);
        return phase is "validate_application_root" or "read_installation_manifest" or "acquire_maintenance_lock"
            or "read_current_state" or "read_current_ticket" or "read_current_journal" or "check_live_helpers"
            or "snapshot_transaction_state" or "enumerate_stages" or "inspect_stage" or "recheck_transaction_state"
            or "remove_stage" or "release_maintenance_lock" ? phase : null;
    }
    private static string? CleanupNativeError(JToken? value)
    {
        if(value?.Type!=JTokenType.Integer)return null;
        var number=value.ToString(Formatting.None);
        return number.Length<=11 && int.TryParse(number,System.Globalization.NumberStyles.AllowLeadingSign,
            System.Globalization.CultureInfo.InvariantCulture,out _) ? number : null;
    }
    private static string CleanupDiagnostics(JObject result)
    {
        var fields=new System.Collections.Generic.List<string>();
        if(CleanupDiagnosticToken(result["error_code"]) is string code)fields.Add("代码 "+code);
        if(CleanupDiagnosticPhase(result["phase"]) is string phase)fields.Add("阶段 "+phase);
        if(CleanupDiagnosticToken(result["error_type"]) is string type)fields.Add("类型 "+type);
        if(CleanupNativeError(result["errno"]) is string errno)fields.Add("errno "+errno);
        if(CleanupNativeError(result["winerror"]) is string winerror)fields.Add("winerror "+winerror);
        return fields.Count>0 ? " 诊断："+string.Join("，",fields)+"。" : " 未返回可用的错误代码或阶段。";
    }
    private static string CacheCleanupSummary(JToken? value)
    {
        if(value is not JObject result)return "更新缓存：未取得清理结果，是否完成尚未确认。";
        if(result["dry_run"]?.Type==JTokenType.Boolean && (bool)result["dry_run"]!)return "更新缓存：本次仅检查，未执行清理。";
        var removed=CleanupCount(result["removed_stages"]);
        var text=removed>0 ? $"更新缓存：已清理 {removed} 个旧更新暂存。"
            : removed==0 ? "更新缓存：本次未删除旧更新暂存。" : "更新缓存：未返回有效的清理数量，是否完成尚未确认。";
        var status=CleanupCode(result["status"]);
        text+=status switch {
            null or ""=>"",
            "process_check_unavailable"=>" 更新助手状态暂时无法确认，缓存已保留。",
            "cleanup_unavailable"=>" 本次缓存整理未完成。",
            "cleanup_busy"=>MaintenanceRetry(result) ? " 另一项缓存整理正在进行，已标记待重试。" : " 另一项缓存整理正在进行，本次尚未完成。",
            "source_tree_not_a_client"=>" 当前是源码工作区，已跳过整理。",
            "linked_application_root" or "linked_update_state"=>" 安装或更新目录包含链接，已跳过整理。",
            _=>" 返回了未识别的整理状态，尚不能确认已完成。"
        };
        if(status is "cleanup_unavailable" or "cleanup_busy")text+=CleanupDiagnostics(result);
        if(result["budget_exhausted"]?.Type==JTokenType.Boolean && (bool)result["budget_exhausted"]!)
            text+=" 本次整理已达到时间预算，仍可能有旧缓存待处理。";
        if(result["protected"] is JArray entries && entries.Count>0) {
            var reasons=entries.OfType<JObject>().Select(item=>CleanupProtectedReason(item["reason"])).GroupBy(reason=>reason).Take(3).Select(group=>$"{group.Count()} 项{group.Key}").ToArray();
            text+=reasons.Length>0 ? " 保留："+string.Join("、",reasons)+"等内容。" : " 部分暂存已保留，原因未能读取。";
        }
        return text;
    }
    private static bool MaintenanceBudget(JToken? value)=>value is JObject result && result["budget_exhausted"]?.Type==JTokenType.Boolean && (bool)result["budget_exhausted"]!;
    private static bool MaintenanceRetry(JToken? value)=>value is JObject result && CleanupCode(result["status"])=="cleanup_busy"
        && result["retry_pending"]?.Type==JTokenType.Boolean && (bool)result["retry_pending"]!;
    private static long AddMaintenanceCount(long total,JToken? value)
    {
        var next=CleanupCount(value);return next is long count && count<=long.MaxValue-total ? total+count : total;
    }
    private static string PacerCleanupSummary(JToken? value)
    {
        if(value is not JObject result)return "过程图：当前后台未返回维护结果，是否完成尚未确认。";
        var skipped=CleanupCode(result["skipped"]);
        if(skipped=="not_installed")return "过程图：未安装 Pacer，无需整理。";
        if(result["dry_run"]?.Type==JTokenType.Boolean && (bool)result["dry_run"]!)return "过程图：本次仅检查，未执行删除。";
        var removed=CleanupCount(result["removed"]);
        var text=removed>0 ? $"过程图：已删除 {removed} 张确认过期的过程图。"
            : removed==0 ? "过程图：本次未删除过程图。" : "过程图：未返回有效的维护数量，是否完成尚未确认。";
        text+=skipped switch {
            null or ""=>"",
            "recent_sweep"=>" 近期已检查，本次跳过。",
            "module_unavailable"=>" 维护模块暂不可用，文件已保留。",
            "maintenance_unavailable"=>" 本次整理未完成，文件已保留。",
            "source_tree_not_a_client" or "not_packaged_backend"=>" 当前不是可维护的客户端目录，已跳过。",
            "linked_backend_path" or "linked_maintenance_state" or "linked_completed_run" or "run_outside_backend"=>" 目录范围暂时无法确认，文件已保留。",
            _=>" 返回了未识别的维护状态，尚不能确认已完成。"
        };
        if(CleanupCount(result["protected_runs"]) is long protectedRuns && protectedRuns>0)text+=$" 保留 {protectedRuns} 局仍在使用或尚未确认结束的记录。";
        if(CleanupCount(result["kept_runs"]) is long kept && kept>0)text+=$" {kept} 局沿用原留图设置。";
        if(CleanupCount(result["errors"]) is long errors && errors>0)text+=$" 有 {errors} 项整理未完成。";
        if(MaintenanceBudget(result))text+=" 本轮整理已达到时间预算，仍有内容待检查。";
        return text;
    }
    private void ApplyMaintenanceResult(JObject result,bool infoResult)
    {
        if(result.Property("cache_cleanup")!=null) { cacheCleanupStatus.Text=CacheCleanupSummary(result["cache_cleanup"]);cacheCleanupStatus.IsVisible=true; }
        else if(infoResult) { cacheCleanupStatus.Text="更新缓存：当前后台未返回清理结果，是否完成尚未确认。";cacheCleanupStatus.IsVisible=true; }
        if(result.Property("pacer_cleanup")!=null) { pacerCleanupStatus.Text=PacerCleanupSummary(result["pacer_cleanup"]);pacerCleanupStatus.IsVisible=true; }
        else if(infoResult) { pacerCleanupStatus.Text="过程图：当前后台未返回维护结果，是否完成尚未确认。";pacerCleanupStatus.IsVisible=true; }
    }
    private static bool MaintenanceIssue(JObject result)
    {
        if((bool?)result["ok"]!=true || result["cache_cleanup"] is not JObject cache || result["pacer_cleanup"] is not JObject pacer)return true;
        if(CleanupCount(cache["removed_stages"])==null || !string.IsNullOrEmpty(CleanupCode(cache["status"])))return true;
        if(cache["protected"] is JArray entries && entries.OfType<JObject>().Any(item=>CleanupCode(item["reason"]) is not ("current_rollback" or "current_or_future_prepared_update")))return true;
        var skipped=CleanupCode(pacer["skipped"]);
        return skipped is not (null or "" or "not_installed" or "recent_sweep") || CleanupCount(pacer["errors"])>0;
    }
    private static void MaintenanceNotice(Action<string>? notice,string text)
    {
        try { notice?.Invoke(text); }catch { /* A notification cannot stop normal startup. */ }
    }
    public Task RunStartupMaintenanceOnceAsync(CancellationToken stopped=default,Action<string>? notice=null,
        Func<TimeSpan,CancellationToken,Task>? wait=null)
        =>startupMaintenanceTask ??= RunStartupMaintenanceAsync(stopped,notice,wait??((duration,token)=>Task.Delay(duration,token)));
    private async Task RunStartupMaintenanceAsync(CancellationToken stopped,Action<string>? notice,Func<TimeSpan,CancellationToken,Task> wait)
    {
        long cacheTotal=0,imagesTotal=0;bool notified=false;
        while(!stopped.IsCancellationRequested) {
            startupMaintenanceStatus.Text="本次启动自动整理：正在检查旧更新缓存和过期过程图…";startupMaintenanceStatus.IsVisible=true;
            try {
                // This is an independent suite-info request, never the Pacer control channel.
                // Closing stops future passes, but does not kill an in-flight directory deletion.
                var result=await request(new JObject {["action"]="info",["maintenance"]=true});
                if(stopped.IsCancellationRequested)return;
                ApplyMaintenanceResult(result,true);
                cacheTotal=AddMaintenanceCount(cacheTotal,result["cache_cleanup"]?["removed_stages"]);
                imagesTotal=AddMaintenanceCount(imagesTotal,result["pacer_cleanup"]?["removed"]);
                bool more=(bool?)result["ok"]==true && (MaintenanceBudget(result["cache_cleanup"]) || MaintenanceBudget(result["pacer_cleanup"]) || MaintenanceRetry(result["cache_cleanup"]));
                bool issue=MaintenanceIssue(result);
                startupMaintenanceStatus.Text=$"本次启动自动整理累计：{cacheTotal} 个旧更新暂存，{imagesTotal} 张过期过程图。"
                    +(more?" 约一分钟后继续检查。":issue?" 部分结果未确认或内容已保留，请查看维护说明。":" 本轮检查已结束，保留项不受影响。");
                if(issue&&!notified){MaintenanceNotice(notice,"自动整理有未完成或保留内容，可在更新／反馈查看原因。");notified=true;}
                if(!more) {
                    if(!notified&&(cacheTotal>0||imagesTotal>0))MaintenanceNotice(notice,startupMaintenanceStatus.Text);
                    return;
                }
                await wait(TimeSpan.FromSeconds(65),stopped);
            }catch(OperationCanceledException) when(stopped.IsCancellationRequested){return;}
            catch {
                if(stopped.IsCancellationRequested)return;
                startupMaintenanceStatus.Text="本次启动自动整理未完成，不影响程序使用；可在更新／反馈重新检查。";
                cacheCleanupStatus.Text="更新缓存：本轮清理结果未确认。";cacheCleanupStatus.IsVisible=true;
                pacerCleanupStatus.Text="过程图：本轮维护结果未确认。";pacerCleanupStatus.IsVisible=true;
                MaintenanceNotice(notice,"自动整理暂未完成，不影响启动；可在更新／反馈查看。");return;
            }
        }
    }
    public static void AttachStartupMaintenance(Window window,Func<MaaUmaActivationView> resolve,Action<string>? notice=null,
        Func<TimeSpan,CancellationToken,Task>? wait=null)
    {
        var lifetime=new CancellationTokenSource();var stopped=lifetime.Token;bool opened=false;
        window.Opened+=(_,_)=>{if(opened)return;opened=true;_=Start();};
        window.Closed+=(_,_)=>{lifetime.Cancel();lifetime.Dispose();};
        async Task Start() {
            try {await resolve().RunStartupMaintenanceOnceAsync(stopped,notice,wait);}
            catch(OperationCanceledException) when(stopped.IsCancellationRequested){}
            catch {if(!stopped.IsCancellationRequested)MaintenanceNotice(notice,"自动整理未能启动，不影响程序使用；可在更新／反馈重新检查。");}
        }
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
        if(busy)return;var allowAutomaticPrepare=SettingsSaved;Busy(true);
        cacheCleanupStatus.Text="更新缓存：正在检查旧更新暂存…";cacheCleanupStatus.IsVisible=true;
        try {
            var result=await request(new JObject {["action"]="info",["maintenance"]=true});Apply(result);
            if(allowAutomaticPrepare && (bool?)result["ok"]==true && SettingsSaved && pacerAuthorized && !pacerInstalled && ticket==null && !autoPrepared){
                autoPrepared=true;Apply(await RequestPreparation(new JObject {["action"]="prepare_pacer"}));
            }
        }catch{message.Text="版本信息读取未完成，可以重试；当前程序与任务未修改。";cacheCleanupStatus.Text="更新缓存：未取得本次清理结果，是否完成尚未确认。";}
        finally{Busy(false);await UpdateIdle();}
    }
    public async Task Configure()
    {
        if(busy)return;Busy(true);ticket=null;
        try {
            var result=await request(new JObject {["action"]="configure",["base_url"]=(address.Text??"").Trim(),["channel"]=SelectedChannel,["download_source"]=SelectedDownloadSource});
            Apply(result);autoPrepared=false;
            if((bool?)result["ok"]==true)Apply(await request(new JObject {["action"]="info"}));
        }
        catch{message.Text="设置保存未确认，请检查后重试。";}finally{Busy(false);await UpdateIdle();}
    }
    public async Task Activate()
    {
        if(busy || !SettingsSaved || string.IsNullOrWhiteSpace(code.Text) || !ActivationFlight.Wait(0))return;
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
    public async Task CheckConnectivity()
    {
        if(busy||!SettingsSaved)return;Busy(true);
        message.Text="正在检查版本服务与下载连接；不会修改设置、授权或安装。";
        try {message.Text=MaaUmaUpdateConnectivity.DiagnosticText(await request(new JObject {["action"]="check_update_connectivity"}));}
        catch {message.Text="网络检查未完成；当前设置、授权和安装未修改。";}
        finally {Busy(false);}
    }
    public async Task PrepareUpdate()
    {
        if(busy || !SettingsSaved)return;Busy(true);ticket=null;
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
        if(busy || ticket==null || !SettingsSaved)return;Busy(true);
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
