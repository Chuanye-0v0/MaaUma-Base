using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Views.UserControls;
using Newtonsoft.Json.Linq;
using SukiUI.Extensions;

namespace MFAAvalonia.Helper;

public interface IUmaPortraitChoice
{
    string Label { get; }
    string IconPath { get; }
    string Description { get; }
    string SearchText { get; }
}

public sealed record UmaTraineeChoice(string CardId, string CharacterName, string Label,
    string IconPath, string Description, string Aliases="") : IUmaPortraitChoice
{
    public bool Manual => CardId=="validation";
    public bool CutPreset => CardId is "101001" or "103102" or "106501";
    public string SearchText => Label+" "+CharacterName+" "+Aliases+" "+Description;
    public override string ToString()=>Label;
}

/// <summary>Reuse UMAA's plant catalog, portraits, DisplayIcon and searchable ComboBox.</summary>
public static class UmaTraineePicker
{
    public static IReadOnlyList<UmaTraineeChoice> Load(string appDirectory, string? repoDirectory=null)
    {
        var result=new List<UmaTraineeChoice>();
        var file=Path.Combine(appDirectory,"interface.json");
        if(File.Exists(file)) {
            if(repoDirectory==null && File.Exists(Path.Combine(appDirectory,"local_backend.json")))
                repoDirectory=(string?)JObject.Parse(File.ReadAllText(Path.Combine(appDirectory,"local_backend.json")))["repo"];
            var rosterPath=repoDirectory==null ? Path.Combine(appDirectory,"backend","core","autonomous_training","plant_roster.json") :
                Path.Combine(repoDirectory,"core","autonomous_training","plant_roster.json");
            var roster=File.Exists(rosterPath) ?
                (JArray?)JObject.Parse(File.ReadAllText(rosterPath))["records"] ?? new JArray() : new JArray();
            var byId=roster.OfType<JObject>().ToDictionary(row=>(string)row["id"]!);
            var cases=(JArray?)JObject.Parse(File.ReadAllText(file))["option"]?["育成马娘"]?["cases"] ?? new JArray();
            foreach(var item in cases.OfType<JObject>()) {
                // The shared icon filename is the育成 card ID, not its costume ID.
                var match=Regex.Match((string?)item["icon"] ?? "",@"(?:^|[/\\])(\d{6})\.png$");
                if(!match.Success)continue;
                var id=match.Groups[1].Value;
                if(result.Any(row=>row.CardId==id))continue;
                byId.TryGetValue(id,out var identity);
                var label=(string?)item["label"] ?? (string?)item["name"] ?? id;
                var name=(string?)identity?["name"] ?? label.Split(" · ")[0].Replace("（初始）","");
                if(identity!=null && !string.IsNullOrWhiteSpace(name)) {
                    var separator=label.IndexOf(" · ",StringComparison.Ordinal);
                    label=separator>=0 ? name+label[separator..] : label.EndsWith("（初始）",StringComparison.Ordinal)
                        ? name+"（初始）" : name+(string.IsNullOrWhiteSpace((string?)identity["outfit"])?"":" · "+identity["outfit"]);
                }
                var description=(string?)item["description"] ?? "";
                result.Add(new(id,name,label,Path.Combine(appDirectory,"resource","portraits",id+".png"),
                    description,string.Join(" ",identity?["aliases"]?.Values<string>() ?? Array.Empty<string>())));
            }
        }
        result.Add(new("validation","","其他角色（手动填写）","✎","仅当共享名单中没有当前角色时手动填写。"));
        return result;
    }

    public static void Configure<T>(ComboBox box, IReadOnlyList<T> choices) where T:IUmaPortraitChoice
    {
        box.ItemsSource=choices;
        box.MinWidth=280;
        box.MaxDropDownHeight=560;
        box.HorizontalAlignment=HorizontalAlignment.Stretch;
        ScrollViewer.SetVerticalScrollBarVisibility(box,ScrollBarVisibility.Visible);
        // These are the same search/input handlers used by UMAA's 自动育成 list.
        ComboBoxExtensions.SetDisableNavigationOnLostFocus(box,true);
        ComboBoxExtensions.SetCanSearch(box,true);
        ComboBoxExtensions.SetSearchMemberPath(box,nameof(IUmaPortraitChoice.SearchText));
        ComboBoxExtensions.SetSearchWatermark(box,"搜索角色、衣装或别名");
        box.ItemTemplate=new FuncDataTemplate<T>((choice,_)=>Item(choice));
        box.SelectionBoxItemTemplate=new FuncDataTemplate<T>((choice,_)=>Item(choice));
    }

    private static Control Item(IUmaPortraitChoice? choice)
    {
        var grid=new Grid { ColumnDefinitions=new("Auto,*"), MinHeight=44,
            HorizontalAlignment=HorizontalAlignment.Stretch };
        var portrait=new DisplayIcon { IconSource=choice?.IconPath, IconSize=40,
            Margin=new Thickness(0,0,8,0), VerticalAlignment=VerticalAlignment.Center };
        grid.Children.Add(portrait);
        var text=new TextBlock { Text=choice?.Label, TextTrimming=TextTrimming.CharacterEllipsis,
            VerticalAlignment=VerticalAlignment.Center };
        Grid.SetColumn(text,1); grid.Children.Add(text);
        ToolTip.SetTip(grid,choice?.Label+"\n"+choice?.Description);
        return grid;
    }
}
