using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

// MaaUma native stop diagnostics button v1; private enhanced UI only.
public sealed class MaaUmaNativeStopDiagnosticsButton : Button
{
    private Func<JObject>? target;
    private Func<JObject,Task<JObject>>? request;
    private bool busy;

    public MaaUmaNativeStopDiagnosticsButton()
    {
        Name="PacerExportStopDiagnostics";Content="导出本次诊断";IsEnabled=false;
        ToolTip.SetTip(this,"只读导出当前局保留的旧失败记录和失败图，不暂停任务，也不会自动上传。");
        Click+=async(_,_)=>await ExportCurrentAsync();
    }

    public void Bind(Func<JObject> target,Func<JObject,Task<JObject>> request)
    {
        this.target=target;this.request=request;RefreshAvailability();
    }

    public void RefreshAvailability()
    {
        try { IsEnabled=!busy&&request!=null&&RunId(target?.Invoke())!=null; }
        catch { IsEnabled=false; }
    }

    private static string? Text(JObject? value,string key)=>value?[key] is JValue {Type:JTokenType.String} token ? token.Value<string>() : null;
    private static string? RunId(JObject? value)
    {
        var id=Text(value,"run_id");
        return id!=null&&Regex.IsMatch(id,@"\A[A-Za-z0-9][A-Za-z0-9_-]{0,159}\z") ? id : null;
    }
    private static string? NameFromPath(string? value)
    {
        if(string.IsNullOrWhiteSpace(value))return null;
        try { return Path.GetFileName(value.TrimEnd(Path.DirectorySeparatorChar,Path.AltDirectorySeparatorChar)); }
        catch { return null; }
    }

    public static JObject Target(JObject? snapshot,JObject? currentRecord,string mode,string endpoint)
    {
        var record=snapshot?["record"] as JObject;
        var cut=snapshot?["cut"] as JObject;
        var names=new[] {Text(record,"run_session_id"),Text(record,"diagnostic_run_id"),
            NameFromPath(Text(cut,"current_attempt_root")),NameFromPath(Text(snapshot,"run_root")),
            NameFromPath(Text(currentRecord,"path"))};
        var id=names.FirstOrDefault(value=>value!=null&&Regex.IsMatch(value,@"\A[A-Za-z0-9][A-Za-z0-9_-]{0,159}\z"));
        return new JObject { ["action"]="export_stop_diagnostics",["mode"]=mode,["endpoint"]=endpoint,["run_id"]=id };
    }

    public async Task<Window?> ExportCurrentAsync()
    {
        if(busy||request==null||target==null)return null;
        JObject current;
        try { current=target(); } catch { return null; }
        if(RunId(current)==null)return null;
        busy=true;IsEnabled=false;
        try {
            // A new request contains an ID only; no source/output path or task-control action.
            var response=await request(new JObject { ["action"]="export_stop_diagnostics",
                ["mode"]=Text(current,"mode"),["endpoint"]=Text(current,"endpoint"),["run_id"]=RunId(current) });
            var window=ResultWindow(response);
            if(TopLevel.GetTopLevel(this) is Window owner&&owner.IsVisible)window.Show(owner);else window.Show();
            return window;
        } catch {
            ToolTip.SetTip(this,"诊断导出未完成，可稍后重试。当前任务及游戏状态未改变。");
            return null;
        } finally { busy=false;RefreshAvailability(); }
    }

    public static Window ResultWindow(JObject response)
    {
        var ok=response["ok"] is JValue {Type:JTokenType.Boolean} flag&&flag.Value<bool>();
        var path=Text(response,"zip_path");
        var localZip=ok&&path!=null&&Path.IsPathFullyQualified(path)&&string.Equals(Path.GetExtension(path),".zip",StringComparison.OrdinalIgnoreCase);
        var window=new Window {Title="本次诊断导出",Width=720,Height=330,MinWidth=480,MinHeight=250,
            WindowStartupLocation=WindowStartupLocation.CenterOwner};
        var location=new TextBox {Name="PacerDiagnosticsZipPath",IsReadOnly=true,Text=localZip?path:"",TextWrapping=TextWrapping.Wrap};
        var message=Text(response,"message")??(ok?"诊断ZIP已生成。":"暂时无法导出诊断，请稍后重试。");
        var copy=new Button {Name="PacerCopyDiagnosticsZipPath",Content="复制压缩包位置",IsEnabled=localZip};
        copy.Click+=async(_,_)=> {try {if(window.Clipboard is { } clipboard&&localZip)await clipboard.SetTextAsync(path!);}catch { }};
        var open=new Button {Name="PacerOpenDiagnosticsFolder",Content="打开所在文件夹",IsEnabled=localZip};
        open.Click+=(_,_)=> {
            try {var folder=localZip?Path.GetDirectoryName(path):null;
                if(folder!=null&&Directory.Exists(folder))Process.Start(new ProcessStartInfo(folder) {UseShellExecute=true});
            }catch { }
        };
        var close=new Button {Content="关闭"};close.Click+=(_,_)=>window.Close();
        window.Content=new StackPanel {Margin=new Thickness(24),Spacing=14,Children={
            new TextBlock {Name="PacerDiagnosticsExportMessage",Text=message,TextWrapping=TextWrapping.Wrap},location,
            new TextBlock {Text="ZIP只保存在本机，可能包含原始游戏画面。请查看后再发给维护者；不会自动上传，也不会暂停当前任务。",TextWrapping=TextWrapping.Wrap},
            new WrapPanel {Children={copy,open,close}}
        }};
        return window;
    }
}
