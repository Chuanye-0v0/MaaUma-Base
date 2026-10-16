using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Extensions.MaaFW;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public static class UmaSeedCycle
{
    private static readonly ConcurrentDictionary<string,bool> RequestedModes = new(StringComparer.Ordinal);
    public static bool IsResumeRequested(string instanceId) =>
        !string.IsNullOrWhiteSpace(instanceId) && RequestedModes.TryGetValue(instanceId,out var requested) && requested;
    public static void SetRequestedMode(string instanceId,bool resumeCycle) {
        if(string.IsNullOrWhiteSpace(instanceId)) {
            if(resumeCycle)throw new InvalidOperationException("连续育成实例未确定，未启动。");
            return;
        }
        if(resumeCycle)RequestedModes[instanceId]=true;
        else RequestedModes.TryRemove(instanceId,out _);
    }
    public static void Inject(MaaProcessor.NodeAndParam node,string entry,JObject options) {
        var value=JToken.Parse(node.Param ?? "[]");
        IEnumerable<JToken> entries=value is JArray array ? array.Children() : new[]{value};
        JObject? target=null;
        foreach(var part in entries)if(part[entry] is JObject row) {
            target=row;
            if(row["custom_action_param"] is JObject old)
                foreach(var key in options.Properties().Select(p=>p.Name).ToArray())old.Remove(key);
        }
        if(target==null) {
            target=new JObject();var extra=new JObject { [entry]=target };
            if(value is JArray rows)rows.Add(extra);else value=new JArray(value,extra);
        }
        if(target["custom_action_param"] is not JObject)target["custom_action_param"]=new JObject();
        foreach(var field in options.Properties())target["custom_action_param"]![field.Name]=field.Value.DeepClone();
        node.Param=value.ToString(Formatting.None);
    }
    // Old public .3 saved a second, standalone seed task. Keep the user's
    // ordinary Plant options when both were checked; never start two owners.
    public static void NormalizeLegacySelection(List<MaaProcessor.NodeAndParam> nodes) {
        var legacy=nodes.Where(n=>n.Entry=="UmaSeedCycle").ToArray();
        if(legacy.Length==0)return;
        if(nodes.Any(n=>n.Entry=="UmaPlant")) {
            foreach(var node in legacy)nodes.Remove(node);
            return;
        }
        if(legacy.Length!=1)
            throw new InvalidOperationException("旧连续育成重复了，请只保留一个开种任务；未执行游戏动作。");
        var selected=legacy[0];
        var value=JToken.Parse(selected.Param ?? "[]");var rows=value is JArray array ? array : new JArray(value);
        foreach(var part in rows.OfType<JObject>())if(part["UmaSeedCycle"] is JObject old) {
            part.Remove("UmaSeedCycle");
            if(old["custom_action"] is JValue action)old["custom_action"]=((string)action!).Replace("UmaSeedCycle","UmaPlant");
            if(old["custom_action_param"] is JObject parameters && parameters["plant"] is JObject plant) {
                var restored=(JObject)plant.DeepClone();
                if(parameters["_execute"]!=null)restored["_execute"]=parameters["_execute"]!.DeepClone();
                old["custom_action_param"]=restored;
            }
            part["UmaPlant"]=old;
        }
        selected.Entry="UmaPlant";selected.Param=rows.ToString(Formatting.None);
    }
    public static void OrderIntegratedSelection(List<MaaProcessor.NodeAndParam> nodes,int count) {
        if(count<1 || count>100)throw new InvalidOperationException("育成次数须为1到100次；未执行游戏动作。");
        var plants=nodes.Where(n=>n.Entry=="UmaPlant").ToArray();
        if(plants.Length>1)throw new InvalidOperationException("请只保留一个开种任务，在开种设置中保存育成次数；未执行游戏动作。");
        if(count>1 && plants.Length==1) {
            // Existing harvest/daily tasks run once before the finite loop.
            // Its goal-stop cannot cut off later daily tasks or double-harvest.
            nodes.Remove(plants[0]);nodes.Add(plants[0]);
        }
    }
    public static void Apply(List<MaaProcessor.NodeAndParam> nodes) => Apply(nodes,"");
    public static void Apply(List<MaaProcessor.NodeAndParam> nodes,string instanceId) {
        var resumeCycle=!string.IsNullOrWhiteSpace(instanceId) && RequestedModes.TryRemove(instanceId,out var requested) && requested;
        var plants=nodes.Where(n=>n.Entry=="UmaPlant").ToArray();
        if(plants.Length==0) {
            if(resumeCycle)throw new InvalidOperationException("请选择开种以继续上次连续育成；未启动。");
            return;
        }
        if(plants.Length!=1)throw new InvalidOperationException("请只保留一个开种任务；未启动。");
        if(resumeCycle) {
            var snapshot=ReadResumeSnapshot();
            ConvertPlantNode(plants[0]);
            Inject(plants[0],"UmaSeedCycle",snapshot);
            nodes.Clear();nodes.Add(plants[0]);
            return;
        }
        var path=Path.Combine(UmaPublicRuntime.BackendRoot,"runs","seed-cycle-settings.json");
        var cycle=File.Exists(path) ? JObject.Parse(File.ReadAllText(path)) : new JObject { ["max_cycles"]=1,["targets"]=new JArray() };
        cycle["integrated"]=true;cycle["adopt_existing"]=false;cycle["resume"]=false;
        var count=(int?)cycle["max_cycles"] ?? 1;
        OrderIntegratedSelection(nodes,count);
        var harvest=new JObject { ["learn_skills"]=true,["update_team"]=false,["return_home"]=false };
        var storePath=Path.Combine(UmaPublicRuntime.BackendRoot,"runs","harvest-presets.json");
        if(File.Exists(storePath)) {
            var store=JObject.Parse(File.ReadAllText(storePath));var selection=store["selection"]!;
            var skill=((JArray)store["skills"]!).FirstOrDefault(r=>(string?)r["id"]==(string?)selection["skill_list_id"]);
            var factor=((JArray)store["factors"]!).FirstOrDefault(r=>(string?)r["id"]==(string?)selection["factor_profile_id"]);
            harvest["preset"]=new JObject { ["version"]=1,["skill_list"]=skill?.DeepClone(),["factor_profile"]=factor?.DeepClone(),
                ["rerolls"]=selection["rerolls"]!.DeepClone(),["tp_budget"]=selection["tp_budget"]!.DeepClone(),["jewel_budget"]=selection["jewel_budget"]!.DeepClone() };
        }
        foreach(var node in plants) {
            ConvertPlantNode(node);
            Inject(node,"UmaSeedCycle",new JObject { ["cycle"]=cycle,["harvest"]=harvest });
        }
    }
    private static JObject ReadResumeSnapshot() {
        var connection=JObject.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory,"config","public-connection.json")));
        var endpoint=UmaPublicRuntime.NormalizeAddress((string?)connection["endpoint"]);
        var port=endpoint.Split(':').Last();
        var scope=Path.GetFullPath(Path.Combine(UmaPublicRuntime.BackendRoot,"runs","seed-cycles",port));
        var latest=Path.Combine(scope,"latest.json");
        if(!File.Exists(latest))throw new InvalidOperationException("当前设备没有可继续的连续育成，请使用开始。");
        var pointer=JObject.Parse(File.ReadAllText(latest));
        var directory=Path.GetFullPath((string?)pointer["directory"] ?? throw new InvalidOperationException("旧任务记录目录缺失，未启动。"));
        if(!string.Equals(Path.GetDirectoryName(directory),scope,StringComparison.OrdinalIgnoreCase)
            || !System.Text.RegularExpressions.Regex.IsMatch(Path.GetFileName(directory),@"^[0-9]+$"))
            throw new InvalidOperationException("旧任务记录目录无效，未启动。");
        var state=JObject.Parse(File.ReadAllText(Path.Combine(directory,"state.json")));
        if((string?)state["endpoint"]!=endpoint)throw new InvalidOperationException("旧连续育成不属于当前设备，未启动。");
        if((string?)state["status"] is "completed" or "ended")
            throw new InvalidOperationException("上次连续育成已结束；请使用开始，按本次设置重新检查当前界面。");
        if(state["config"] is not JObject config || config["cycle"] is not JObject
            || config["plant"] is not JObject || config["harvest"] is not JObject)
            throw new InvalidOperationException("旧连续育成设置不完整，未启动。");
        var snapshot=(JObject)config.DeepClone();
        snapshot["cycle"]!["resume"]=true;
        return snapshot;
    }
    private static void ConvertPlantNode(MaaProcessor.NodeAndParam node) {
        var value=JToken.Parse(node.Param ?? "[]");var rows=value is JArray array ? array : new JArray(value);
        foreach(var part in rows.OfType<JObject>())if(part["UmaPlant"] is JObject old) {
            part.Remove("UmaPlant");
            if(old["custom_action"] is JValue action)old["custom_action"]=((string)action!).Replace("UmaPlant","UmaSeedCycle");
            if(old["custom_action_param"] is JObject parameters) {
                var execute=parameters["_execute"]?.DeepClone();parameters.Remove("_execute");
                old["custom_action_param"]=new JObject { ["plant"]=parameters };
                if(execute!=null)old["custom_action_param"]!["_execute"]=execute;
            }
            part["UmaSeedCycle"]=old;
        }
        node.Param=rows.ToString(Formatting.None);node.Entry="UmaSeedCycle";node.Count=1;
    }
    private static void InjectPlantSupport(MaaProcessor.NodeAndParam node,JObject support) {
        var value=JToken.Parse(node.Param ?? "[]");var parts=value is JArray a ? a : new JArray(value);
        JObject? target=null;
        foreach(var part in parts)if(part["UmaSeedCycle"]?["custom_action_param"] is JObject options) {
            target=options;
            if(options["plant"] is JObject plant)foreach(var field in support.Properties())plant.Remove(field.Name);
        }
        if(target==null)throw new InvalidOperationException("连续任务参数未生成。");
        if(target["plant"] is not JObject)target["plant"]=new JObject();
        foreach(var field in support.Properties())target["plant"]![field.Name]=field.Value.DeepClone();
        node.Param=parts.ToString(Formatting.None);
    }
    public static Control CreateControls(Func<bool> running,Action? resumeCycle=null) {
        var root=new StackPanel { Spacing=8,Margin=new Thickness(0,8,0,16) };
        var title=new TextBlock { Text="育成次数",FontSize=15 };
        var count=new NumericUpDown { Minimum=1,Maximum=100,Value=1,FormatString="0",Width=120 };
        var save=new Button { Content="保存育成次数" };
        var hint=new TextBlock { Text="开始按本次设置重新检查当前界面；只有继续按钮才恢复旧连续进度。1次只开种，2次及以上按收菜设置循环种收、达标提前停止。另勾选的日活先执行一次。缺TP先用药，确认无药才用萝卜，不买药。",TextWrapping=TextWrapping.Wrap };
        JObject? data=null;
        root.Children.Add(title);root.Children.Add(count);root.Children.Add(save);root.Children.Add(hint);
        if(resumeCycle!=null) {
            var resume=new Button { Content="继续上次连续育成（仅此项 / 原设置）",HorizontalAlignment=HorizontalAlignment.Stretch };
            resume.Click+=(_,_)=> {
                if(running()){hint.Text="当前任务尚未停止，停止完成后才能继续。";return;}
                try{resumeCycle();}catch(Exception error){hint.Text="继续未启动："+error.Message;}
            };
            root.Children.Add(resume);
        }
        save.Click+=async(_,_)=> {
            if(running()){hint.Text="请停止任务后修改次数；运行中的任务设置保持不变。";return;}
            if(data==null)return;
            var draft=(JObject)data.DeepClone();draft["max_cycles"]=(int)(count.Value ?? 1);draft["integrated"]=true;
            save.IsEnabled=false;
            try {await UmaPublicRuntime.Request(new JObject { ["action"]="save",["settings"]=draft },"desktop.umaa_seed_cycle");data=draft;hint.Text="已保存；直接勾选开种，再点右上角开始。技能和因子沿用收菜设置。";}
            catch(Exception error){hint.Text="未保存："+error.Message;}
            finally{save.IsEnabled=true;}
        };
        root.AttachedToVisualTree+=async(_,_)=> {
            try {data=(JObject)(await UmaPublicRuntime.Request(new JObject { ["action"]="load" },"desktop.umaa_seed_cycle"))[0]!["settings"]!;count.Value=(decimal)data["max_cycles"]!;}
            catch(Exception error){hint.Text="次数读取失败："+error.Message;save.IsEnabled=false;}
        };
        return root;
    }
    public static Control CreateGoalControls(Func<bool> running) {
        var root=new StackPanel { Spacing=6 };
        var button=new Button { Content="连续育成的达标目标因子（可选）…",HorizontalAlignment=HorizontalAlignment.Stretch };
        var status=new TextBlock { TextWrapping=TextWrapping.Wrap };
        root.Children.Add(button);root.Children.Add(status);
        button.Click+=async(_,_)=> {
            if(running()){status.Text="请停止任务后修改目标；运行中的任务设置保持不变。";return;}
            if(TopLevel.GetTopLevel(button) is not Window owner)return;
            button.IsEnabled=false;status.Text="";
            try {
            var response=(await UmaPublicRuntime.Request(new JObject { ["action"]="load" },"desktop.umaa_seed_cycle"))[0]!;
            var settings=(JObject)response["settings"]!;var factors=(JArray)response["factors"]!;
            var draft=(JArray)settings["targets"]!.DeepClone();
            var window=new Window { Title="收菜 · 连续育成达标条件（全部满足）",Width=850,Height=680,WindowStartupLocation=WindowStartupLocation.CenterOwner };
            var layout=new DockPanel { Margin=new Thickness(18) };window.Content=layout;
            TextBlock Text(string value)=>new(){Text=value,TextWrapping=TextWrapping.Wrap};
            var footer=new StackPanel { Orientation=Orientation.Horizontal,Spacing=10 };
            var save=new Button { Content="保存目标" };var cancel=new Button { Content="取消" };var clear=new Button { Content="不设置目标，仅按次数育成" };
            footer.Children.Add(save);footer.Children.Add(clear);footer.Children.Add(cancel);DockPanel.SetDock(footer,Dock.Bottom);layout.Children.Add(footer);
            var saveStatus=Text("");DockPanel.SetDock(saveStatus,Dock.Bottom);layout.Children.Add(saveStatus);
            var grid=new Grid { ColumnDefinitions=new("*,*"),ColumnSpacing=16 };layout.Children.Add(grid);
            var left=new DockPanel();var search=new TextBox { Watermark="搜索目标因子" };DockPanel.SetDock(search,Dock.Top);left.Children.Add(search);
            var available=new ListBox { SelectionMode=SelectionMode.Multiple,ItemTemplate=new FuncDataTemplate<JObject>((row,_)=>Text((string?)row?["name"] ?? "")) };
            var add=new Button { Content="添加选中因子 →" };DockPanel.SetDock(add,Dock.Bottom);left.Children.Add(add);left.Children.Add(available);grid.Children.Add(left);
            var selected=new StackPanel { Spacing=8 };var scroll=new ScrollViewer { Content=selected };Grid.SetColumn(scroll,1);grid.Children.Add(scroll);
            void Render() {
                selected.Children.Clear();
                foreach(var target in draft.OfType<JObject>().ToArray()) {
                    var key=(string)target["key"]!;var row=new StackPanel { Orientation=Orientation.Horizontal,Spacing=8 };
                    var label=Text((string?)factors.FirstOrDefault(f=>(string?)f["key"]==key)?["name"] ?? key);label.Width=170;
                    var stars=new NumericUpDown { Minimum=1,Maximum=3,Value=(decimal)target["min_stars"]!,FormatString="0",Width=70 };
                    var remove=new Button { Content="移除" };stars.ValueChanged+=(_,_)=>target["min_stars"]=(int)(stars.Value ?? 1);
                    remove.Click+=(_,_)=>{draft.Remove(target);Render();};
                    foreach(var control in new Control[]{label,Text("至少"),stars,Text("星"),remove})row.Children.Add(control);selected.Children.Add(row);
                }
            }
            void Filter()=>available.ItemsSource=factors.OfType<JObject>().Where(f=>((string?)f["name"] ?? "").Contains(search.Text ?? "",StringComparison.OrdinalIgnoreCase)).ToArray();
            search.TextChanged+=(_,_)=>Filter();add.Click+=(_,_)=>{foreach(var row in available.SelectedItems?.OfType<JObject>() ?? Array.Empty<JObject>())if(!draft.Any(t=>(string?)t["key"]==(string?)row["key"]))draft.Add(new JObject { ["key"]=row["key"]!.DeepClone(),["min_stars"]=3 });Render();};
            async Task Save() {
                save.IsEnabled=false;clear.IsEnabled=false;
                try {
                settings["targets"]=draft.DeepClone();settings["integrated"]=true;
                await UmaPublicRuntime.Request(new JObject { ["action"]="save",["settings"]=settings },"desktop.umaa_seed_cycle");window.Close();
                } catch(Exception error){saveStatus.Text="未保存："+error.Message;}
                finally{save.IsEnabled=true;clear.IsEnabled=true;}
            }
            save.Click+=async(_,_)=>await Save();clear.Click+=async(_,_)=>{draft.Clear();await Save();};cancel.Click+=(_,_)=>window.Close();
            Render();Filter();await window.ShowDialog(owner);
            } catch(Exception error){status.Text="目标读取失败："+error.Message;}
            finally{button.IsEnabled=true;}
        };
        return root;
    }
    public static void SelfTest() {
        var node=new MaaProcessor.NodeAndParam { Entry="UmaSeedCycle",Param="[{\"UmaSeedCycle\":{\"custom_action_param\":{\"plant\":{\"trainee\":\"vodka\"},\"cycle\":{\"targets\":[{\"key\":\"old\"}]}}}},{\"UmaSeedCycle\":{\"custom_action_param\":{\"plant\":{\"scenario\":\"arc\"},\"cycle\":{\"targets\":[{\"key\":\"old2\"}]}}}}]" };
        Inject(node,"UmaSeedCycle",new JObject { ["cycle"]=new JObject { ["targets"]=new JArray(new JObject { ["key"]="blue:耐力",["min_stars"]=3 }) } });
        InjectPlantSupport(node,new JObject { ["support_deck"]=3,["borrow_card_id"]=30028 });
        var merged=new JObject();foreach(var part in JArray.Parse(node.Param!))merged.Merge(part);
        var options=merged["UmaSeedCycle"]!["custom_action_param"]!;
        if((string?)options["plant"]?["trainee"]!="vodka" || (string?)options["plant"]?["scenario"]!="arc"
            || (int?)options["plant"]?["support_deck"]!=3 || (int?)options["plant"]?["borrow_card_id"]!=30028
            || ((JArray)options["cycle"]!["targets"]!).Count!=1)
            throw new Exception("连续任务的原开种设置／目标快照合并错误。");
    }
}
