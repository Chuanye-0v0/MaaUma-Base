using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using MFAAvalonia.Extensions.MaaFW;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public static class UmaPublicRuntime
{
    private static FileStream? applicationLock;
    public static void AcquireApplicationLock()
    {
        if(applicationLock!=null)return;
        var folder=Path.Combine(AppContext.BaseDirectory,"updates");Directory.CreateDirectory(folder);
        FileStream? stream=null;
        try {
            stream=new FileStream(Path.Combine(folder,"application.lock"),FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.Read);
            if(stream.Length==0){stream.WriteByte(0);stream.Flush();}
            stream.Lock(0,1);
            applicationLock=stream;
            AppDomain.CurrentDomain.ProcessExit+=(_,_)=>{applicationLock?.Dispose();applicationLock=null;};
        } catch(IOException error) {
            stream?.Dispose();
            throw new IOException("另一窗口或更新正在使用此安装目录，请稍后再打开。",error);
        }
    }
    public static string BackendRoot => Path.Combine(AppContext.BaseDirectory, "backend");
    public static string NormalizeAddress(string? address)
    {
        var local=System.Text.RegularExpressions.Regex.Match(address ?? "", @"^127\.0\.0\.1:([0-9]{1,5})$");
        if(local.Success && int.TryParse(local.Groups[1].Value,out var port) && port>=1 && port<=65535)
            return "127.0.0.1:"+port;
        var emulator=System.Text.RegularExpressions.Regex.Match(address ?? "", @"^emulator-([0-9]{1,5})$");
        if(emulator.Success && int.TryParse(emulator.Groups[1].Value,out var console)
            && console>=1 && console<65535 && console%2==0)
            return "127.0.0.1:"+(console+1);
        throw new InvalidOperationException("请选择本机模拟器的 127.0.0.1:端口 或 emulator-串号。");
    }
    public static async Task Prepare(List<MaaProcessor.NodeAndParam> nodes, string adbPath, string address, string instanceId = "default")
    {
        var endpoint=NormalizeAddress(address);
        if (!File.Exists(adbPath))
            throw new InvalidOperationException("请在设备连接设置中选择本机模拟器的 ADB 设备。");
        var resumeCycle = UmaSeedCycle.IsResumeRequested(instanceId);
        if (!resumeCycle) {
            // Prove the previous current owner is safe before ANY selected daily
            // task can run or any selected task snapshot is changed.
            var proof = await Request(new JObject {
                ["action"]="preflight_start",
                ["connection"]=new JObject { ["endpoint"]=endpoint, ["adb_path"]=adbPath }
            }, "desktop.umaa_seed_cycle");
            if ((bool?)proof[0]?["ready"] != true)
                throw new InvalidOperationException("上次操作尚未核实，新任务未启动。");
        }
        var config = Path.Combine(AppContext.BaseDirectory, "config");
        Directory.CreateDirectory(config);
        var target = Path.Combine(config, "public-connection.json");
        var temporary = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllText(temporary, new JObject { ["endpoint"]=endpoint, ["adb_path"]=adbPath }.ToString());
        File.Move(temporary, target, true);
        UmaSeedCycle.NormalizeLegacySelection(nodes);
        if (!resumeCycle) {
            UmaHarvestPresets.Apply(nodes);
            UmaPublicPlant.Apply(nodes);
        }
        UmaSeedCycle.Apply(nodes, instanceId);
        var runs = Path.Combine(AppContext.BaseDirectory, "runs");
        Directory.CreateDirectory(runs);
        File.WriteAllText(Path.Combine(runs, "queue-generation.txt"), Guid.NewGuid().ToString("N"));
    }

    public static async Task<JArray> Request(JObject request, string module)
    {
        if (module != "desktop.umaa_public_presets" && module != "desktop.umaa_public_plant"
            && module != "desktop.umaa_seed_cycle" && module != "desktop.parent_library_api" && module != "desktop.parent_planner_api") throw new InvalidOperationException("公开编辑接口不可用。");
        var python = Path.Combine(AppContext.BaseDirectory, "runtime", "python.exe");
        var info = new ProcessStartInfo(python) {
            WorkingDirectory=BackendRoot, UseShellExecute=false, CreateNoWindow=true,
            RedirectStandardInput=true, RedirectStandardOutput=true, RedirectStandardError=true,
            StandardInputEncoding=new UTF8Encoding(false), StandardOutputEncoding=Encoding.UTF8,
            StandardErrorEncoding=Encoding.UTF8
        };
        foreach (var argument in new[] {"-B", "-X", "utf8", "-m", module}) info.ArgumentList.Add(argument);
        var temporary = Path.Combine(AppContext.BaseDirectory, "temp"); Directory.CreateDirectory(temporary);
        info.Environment["TEMP"]=temporary; info.Environment["TMP"]=temporary;
        using var process = Process.Start(info) ?? throw new IOException("未能启动本地方案编辑接口。");
        var output = process.StandardOutput.ReadToEndAsync();
        var errors = process.StandardError.ReadToEndAsync();
        await process.StandardInput.WriteAsync(request.ToString(Formatting.None));
        process.StandardInput.Close();
        await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(30));
        var response = await output;
        var diagnostic = await errors;
        if (process.ExitCode != 0) {
            var detail = diagnostic.Split(new[]{'\r','\n'}, StringSplitOptions.RemoveEmptyEntries).LastOrDefault() ?? "";
            if (detail.Length > 300) detail = detail[..300];
            throw new InvalidOperationException($"本地设置进程失败（退出码 {process.ExitCode}）：{detail}");
        }
        if (string.IsNullOrWhiteSpace(response))
            throw new InvalidOperationException("本地设置进程未返回内容，请导出日志确认是否保存。");
        JObject result;
        try { result = JObject.Parse(response); }
        catch (JsonException error) {
            throw new InvalidOperationException("本地设置进程返回格式错误，请导出日志。", error);
        }
        if ((bool?)result["ok"] != true)
            throw new InvalidOperationException((string?)result["message"] ?? (result["error"]?.Type==JTokenType.String?(string?)result["error"]:null) ?? "方案读写失败。");
        return result["results"] as JArray ?? throw new InvalidOperationException("本地设置进程缺少设置内容。");
    }

    public static int SelfTest()
    {
        UmaSeedCycle.SelfTest();
        foreach(var pair in new[]{("emulator-5554","127.0.0.1:5555"),("emulator-5556","127.0.0.1:5557"),
                                 ("127.0.0.1:16416","127.0.0.1:16416")})
            if(NormalizeAddress(pair.Item1)!=pair.Item2)throw new Exception("模拟器串号映射错误");
        foreach(var value in new[]{"192.168.1.1:5555","emulator-5555","emulator-65535","serial-anything"}) {
            try {NormalizeAddress(value);} catch(InvalidOperationException) {continue;}
            throw new Exception("接受了无效或非本机模拟器地址");
        }
        var ownAssembly=typeof(UmaPublicRuntime).Assembly;
        var allowed=new[]{"UmaPublicRuntime","UmaHarvestPresets","UmaPublicUpdateView","UmaPublicPlant","UmaSeedCycle","UmaParentLibraryView","UmaParentLibraryPage","UmaParentPlannerView","UmaParentPlannerPage","UmaParentPlannerBrowser","UmaIconRendering"};
        if(ownAssembly.GetTypes().Any(type=>type.Name.StartsWith("Uma",StringComparison.Ordinal) && !allowed.Contains(type.Name)))
            throw new Exception("公开 GUI 包含未登记的扩展类型。");
        var payload=JObject.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory,"interface.json")));
        if(((JArray)payload["task"]!).Any(task=>(string?)task["entry"]=="UmaBatch"))throw new Exception("包含多账号入口");
        Console.WriteLine("UMAA public GUI assembly and task interface verified; no device discovery or connection.");
        return 0;
    }
}
