using System;
using System.Threading.Tasks;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Linq;
using System.Collections.Generic;
using Avalonia.Controls;
using MFAAvalonia.ViewModels;
using MFAAvalonia.Extensions.MaaFW;
using MaaFramework.Binding;
using MaaFramework.Binding.Buffers;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Navigation adapter only; both hosts use the same file-only library API.</summary>
public sealed class UmaParentLibraryPage : UserControl
{
    private Dictionary<string,JObject> knownDevices=new(StringComparer.Ordinal);
    public UmaParentLibraryPage()=>Content=new UmaParentLibraryView(Send,explicitDeviceSelection:true);

    internal static string NormalizeEndpoint(string? value)
    {
        var local=System.Text.RegularExpressions.Regex.Match(value??"",@"^127\.0\.0\.1:([0-9]{1,5})$");
        if(local.Success&&int.TryParse(local.Groups[1].Value,out var port)&&port is >=1 and <=65535)return "127.0.0.1:"+port;
        var emulator=System.Text.RegularExpressions.Regex.Match(value??"",@"^emulator-([0-9]{1,5})$");
        if(emulator.Success&&int.TryParse(emulator.Groups[1].Value,out var console)&&console is >=1 and <65535&&console%2==0)
            return "127.0.0.1:"+(console+1);
        throw new InvalidOperationException("请在面板中选择本机模拟器后再扫描。");
    }
    internal static JObject WithConnection(JObject input,string? adbPath,string? endpoint,bool running)
    {
        var payload=(JObject)input.DeepClone();
        if((string?)payload["action"]!="parent_library_scan_start")return payload;
        if(running)throw new InvalidOperationException("当前面板仍在运行任务，请停止后再扫描种马库。");
        if(string.IsNullOrWhiteSpace(adbPath))throw new InvalidOperationException("请先设置当前模拟器的 ADB 路径。");
        payload["adb_path"]=adbPath;payload["endpoint"]=NormalizeEndpoint(endpoint);
        payload["owner_pid"]=Environment.ProcessId;return payload;
    }
    internal static JObject BindSelectedTarget(JObject input,IReadOnlyDictionary<string,JObject> devices,IEnumerable<string> busyEndpoints)
    {
        if((string?)input["action"]!="parent_library_scan_start")return (JObject)input.DeepClone();
        var id=(string?)input["device_id"];
        if(string.IsNullOrWhiteSpace(id)||!devices.TryGetValue(id,out var target))
            throw new InvalidOperationException("请在种马库选择本次扫描设备；列表变化后请重新选择。");
        var endpoint=(string?)target["endpoint"];
        var payload=WithConnection(input,(string?)target["adb_path"],endpoint,busyEndpoints.Contains(endpoint));
        payload.Remove("device_id");return payload;
    }
    private static string[] BusyEndpoints()=>Instances.InstanceTabBarViewModel.Tabs
        .Where(tab=>tab.TaskQueueViewModel?.IsRunning==true)
        .Select(tab=>{try{return NormalizeEndpoint(tab.TaskQueueViewModel.Processor.Config.AdbDevice.AdbSerial);}catch{return "";}})
        .Where(value=>value.Length>0).Distinct().ToArray();
    private async Task<JObject> Devices(bool discover)
    {
        var found=new Dictionary<string,JObject>(StringComparer.Ordinal);
        void Add(string? name,string? adb,string? serial,string source){
            if(string.IsNullOrWhiteSpace(adb))return;
            string endpoint;try{endpoint=NormalizeEndpoint(serial);}catch{return;}
            var id=endpoint+"|"+adb;
            found[id]=new JObject{["id"]=id,["name"]=string.IsNullOrWhiteSpace(name)?"本机模拟器":name,
                ["endpoint"]=endpoint,["adb_path"]=adb,["source"]=source};
        }
        foreach(var tab in Instances.InstanceTabBarViewModel.Tabs.ToArray()){
            var vm=tab.TaskQueueViewModel;if(vm==null)continue;
            var saved=vm.Processor.Config.AdbDevice;
            Add(saved.Name,saved.AdbPath,saved.AdbSerial,"配置“"+tab.Name+"”保存的设备");
            foreach(var device in vm.Devices.OfType<AdbDeviceInfo>())Add(device.Name,device.AdbPath,device.AdbSerial,"面板已发现的设备");
        }
        if(discover){
            var actual=await Task.Run(()=>MaaProcessor.Toolkit.AdbDevice.Find());
            foreach(var device in actual)Add(device.Name,device.AdbPath,device.AdbSerial,"当前发现的设备");
        }
        var busy=BusyEndpoints();foreach(var item in found.Values)item["busy"]=busy.Contains((string?)item["endpoint"]);
        knownDevices=found;
        return new JObject{["devices"]=new JArray(found.Values),["selected_id"]=null};
    }
    private async Task<JObject> Send(JObject input)
    {
        if((string?)input["action"]=="parent_library_devices")return await Devices((bool?)input["refresh"]==true);
        var payload=BindSelectedTarget(input,knownDevices,BusyEndpoints());
        var result=await UmaPublicRuntime.Request(payload,"desktop.parent_library_api");
        return result[0] as JObject??throw new InvalidOperationException("种马库没有返回完整结果，请刷新重试。");
    }
    private static async Task<JArray> CallApi(JObject payload,string python,string backend)
    {
        if(!((string?)payload["action"]??"").StartsWith("parent_library_",StringComparison.Ordinal))
            throw new InvalidOperationException("不支持的种马库操作。");
        var info=new ProcessStartInfo(python){WorkingDirectory=backend,UseShellExecute=false,CreateNoWindow=true,
            RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,
            StandardInputEncoding=new UTF8Encoding(false),StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8};
        foreach(var value in new[]{"-B","-X","utf8","-m","desktop.parent_library_api"})info.ArgumentList.Add(value);
        using var process=Process.Start(info)??throw new IOException("种马库服务未启动。");
        var output=process.StandardOutput.ReadToEndAsync();var diagnostic=process.StandardError.ReadToEndAsync();
        await process.StandardInput.WriteAsync(payload.ToString(Newtonsoft.Json.Formatting.None));process.StandardInput.Close();
        await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(30));await diagnostic;
        var response=JObject.Parse(await output);
        if((bool?)response["ok"]!=true)throw new InvalidOperationException((string?)response["message"]??"种马库操作未完成。");
        return response["results"] as JArray??throw new InvalidOperationException("种马库结果不完整。");
    }
}
