using System;
using System.IO;
using System.Text;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

// MaaUma recognition feedback settings v1; enhanced GUI only.
public sealed class MaaUmaRecognitionFeedbackSettingsView : UserControl
{
    public const string SettingsSchema = "maauma.recognition_feedback.settings.v1";
    private readonly string applicationRoot;
    private readonly Func<bool> installed;
    private readonly CheckBox enabled = new() {
        Name="MaaUmaRecognitionFeedbackEnabled",
        Content="育成结束后自动上传识别失败截图"
    };
    private readonly TextBlock status = new() {
        Name="MaaUmaRecognitionFeedbackStatus", TextWrapping=TextWrapping.Wrap
    };
    private bool changing;
    private bool savedEnabled = true;

    public MaaUmaRecognitionFeedbackSettingsView() : this(AppContext.BaseDirectory) { }

    public MaaUmaRecognitionFeedbackSettingsView(string applicationRoot, Func<bool>? installed=null)
    {
        this.applicationRoot=applicationRoot;
        this.installed=installed??(()=>Directory.Exists(Path.Combine(applicationRoot,"pacer")));
        Name="MaaUmaRecognitionFeedbackSettings";
        Content=new StackPanel {Spacing=8,Children={
            enabled,
            new TextBlock {
                Name="MaaUmaRecognitionFeedbackPrivacy",
                Text="默认开启。只上传触发识别失败的原始游戏画面及诊断，可能包含游戏内昵称，仅维护者可见。本局真正结束前只在本地留档。",
                TextWrapping=TextWrapping.Wrap
            },
            new TextBlock {
                Name="MaaUmaRecognitionFeedbackScope",
                Text="关闭后仍保留本地留档。不上传桌面、账号配置或正常游戏帧；更改后立即保存，不影响育成运行。",
                TextWrapping=TextWrapping.Wrap
            },status
        }};
        ReloadSettings();
        enabled.IsCheckedChanged+=(_,_)=>{
            if(!changing)SaveEnabled(enabled.IsChecked==true);
        };
        Loaded+=(_,_)=>RefreshAvailability();
        RefreshAvailability();
    }

    public void RefreshAvailability()
    {
        try { IsVisible=installed(); }
        catch { IsVisible=false; }
    }

    public void ReloadSettings()
    {
        bool warning=false;
        savedEnabled=true;
        try {
            var path=SettingsPath(applicationRoot);
            if(File.Exists(path)) {
                var value=JObject.Parse(File.ReadAllText(path,Encoding.UTF8));
                if((string?)value["schema_version"]!=SettingsSchema || value["enabled"]?.Type!=JTokenType.Boolean)
                    warning=true;
                else savedEnabled=(bool)value["enabled"]!;
            }
        } catch { warning=true; }
        SetChecked(savedEnabled);
        status.Text=warning
            ? "设置暂未读取，当前按默认开启；可重新保存，不影响育成运行。"
            : savedEnabled ? "已开启：本局真正结束后自动私密上传识别失败截图。"
                : "已关闭自动上传；识别失败截图仍保留在本地。";
    }

    private void SaveEnabled(bool value)
    {
        string? temporary=null;
        try {
            var path=SettingsPath(applicationRoot);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            temporary=path+"."+Guid.NewGuid().ToString("N")+".tmp";
            var document=new JObject { ["schema_version"]=SettingsSchema,["enabled"]=value };
            File.WriteAllText(temporary,document.ToString()+"\n",new UTF8Encoding(false));
            File.Move(temporary,path,true);
            temporary=null;
            savedEnabled=value;
            status.Text=value ? "已开启：本局真正结束后自动私密上传识别失败截图。"
                : "已关闭自动上传；识别失败截图仍保留在本地。";
        } catch {
            SetChecked(savedEnabled);
            status.Text="未能保存，当前仍为"+(savedEnabled?"开启":"关闭")+"；可重试，不影响育成运行。";
        } finally {
            if(temporary!=null)try { File.Delete(temporary); } catch { }
        }
    }

    private void SetChecked(bool value)
    {
        changing=true;
        try { enabled.IsChecked=value; }
        finally { changing=false; }
    }

    public static string SettingsPath(string applicationRoot) =>
        Path.Combine(applicationRoot,"config","maauma-recognition-feedback.json");
}
