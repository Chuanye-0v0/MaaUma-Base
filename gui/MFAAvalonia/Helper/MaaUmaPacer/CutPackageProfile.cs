using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
namespace UmaCutPanel;

internal sealed record CutPackageProfile(string Channel,bool ContinuationAssessment,bool DecisionPreferences=false,bool PacerCenter=false)
{
    public static CutPackageProfile Read(string root)
    {
        try {
            var data=JObject.Parse(File.ReadAllText(Path.Combine(root,"product-package-manifest.json")));
            var channel=(string?)data["release_channel"];
            channel=channel is "stable" or "preview" ? channel : (string?)data["distribution"]=="private-preview" ? "preview" : "stable";
            var enabled=(bool?)data["features"]?["continuation_assessment"] ?? (string?)data["version"]=="10.20.0-preview.11";
            return new(channel,channel=="preview" && enabled,
                channel=="preview" && (bool?)data["features"]?["decision_preferences"]==true,
                (string?)data["product_id"]=="uma-cut" && channel=="preview" && (bool?)data["features"]?["pacer_center"]==true);
        } catch {return new("stable",false);}
    }
}
