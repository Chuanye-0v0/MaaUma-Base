using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed record UmaSupportChoice(int CardId,string Label,string IconPath,string Description,string SearchText):IUmaPortraitChoice;

/// <summary>The same card identities/art catalog used by UMAA plant_support.</summary>
public static class UmaSupportPicker
{
    private static string CharacterKey(JObject row)
    {
        var explicitId=row["character_id"]?.ToString();
        if(!string.IsNullOrWhiteSpace(explicitId))return explicitId;
        var id=row["id"]?.ToString();
        return id?.Length==6 && int.TryParse(id,out var costume)?(costume/100).ToString():"";
    }
    private static readonly Dictionary<string,string> Types=new() {
        ["speed"]="速度",["stamina"]="耐力",["power"]="力量",["guts"]="毅力",
        ["intelligence"]="智力",["friend"]="友人",["group"]="小组"
    };
    public static IReadOnlyList<UmaSupportChoice> Load(string appDirectory,string? repoDirectory=null)
    {
        var choices=new List<UmaSupportChoice> { new(0,"沿用首局借卡（需已有记录）","↻","未指定卡片时沿用原有借卡资料。","沿用 首局 当前") };
        if(repoDirectory==null && File.Exists(Path.Combine(appDirectory,"local_backend.json")))
            repoDirectory=(string?)JObject.Parse(File.ReadAllText(Path.Combine(appDirectory,"local_backend.json")))["repo"];
        var path=repoDirectory==null ? Path.Combine(appDirectory,"backend","runs","support-card-library","catalog.json") :
            Path.Combine(repoDirectory,"runs","support-card-library","catalog.json");
        if(!File.Exists(path))return choices;
        var data=JObject.Parse(File.ReadAllText(path));
        if((string?)data["schema"]!="uma.support_card_art.v1")return choices;
        var rosterPath=repoDirectory==null ? Path.Combine(appDirectory,"backend","core","autonomous_training","plant_roster.json") :
            Path.Combine(repoDirectory,"core","autonomous_training","plant_roster.json");
        var names=File.Exists(rosterPath)?((JArray?)JObject.Parse(File.ReadAllText(rosterPath))["records"]??new JArray()):new JArray();
        var aliases=names.OfType<JObject>().GroupBy(CharacterKey).ToDictionary(group=>group.Key,
            group=>string.Join(" ",group.SelectMany(row=>new[]{(string?)row["name"]??""}.Concat(row["aliases"]?.Values<string>().OfType<string>()??Array.Empty<string>())).Distinct()));
        foreach(var card in ((JArray?)data["cards"] ?? new JArray()).OfType<JObject>()) {
            var type=(string?)card["type"] ?? "";
            if((string?)card["image_status"]!="ready" || !Types.ContainsKey(type))continue;
            var id=(int)card["card_id"]!;
            var cnName=((string?)card["name_cn"]??"").Trim();
            var cnTitle=((string?)card["title_cn"]??"").Trim().Trim('[',']');
            var name=cnName.Length>0?cnName:$"国服卡名待核对 #{id}";
            var title=cnTitle.Length>0?cnTitle:cnName.Length>0?$"卡面名待核对 #{id}":"";
            var label=string.Join(" · ",new[]{card["rarity"]?.ToString()??"",Types[type],name,title}.Where(value=>value.Length>0));
            var image=Path.GetFullPath(Path.Combine(Path.GetDirectoryName(path)!, (string)card["image_path"]!));
            aliases.TryGetValue(card["character_id"]?.ToString()??"",out var alternateNames);
            choices.Add(new(id,label,image,title.Length>0?$"{name} [{title}]":name,
                label+" "+alternateNames+" "+card["name_jp"]+" "+card["title_jp"]+" "+card["name_tw"]+" "+card["title_tw"]+" "+id));
        }
        return choices;
    }
}
