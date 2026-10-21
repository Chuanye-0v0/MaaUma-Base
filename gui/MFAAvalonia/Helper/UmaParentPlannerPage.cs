using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using Avalonia.Controls;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Local-only adapter. The website never receives this request callback.</summary>
public sealed class UmaParentPlannerPage : UserControl
{
    public UmaParentPlannerPage(){Content=new UmaParentPlannerView(Send);}
    internal static string ModuleFor(string? action)=>action switch
    {
        "parent_planner_catalog" or "parent_planner_evaluate" or "parent_planner_skill_search" or "parent_planner_skill_detail" => "desktop.parent_planner_api",
        "parent_compatibility_catalog" or "parent_compatibility_evaluate" or "parent_compatibility_suggest" => "desktop.parent_compatibility_api",
        "team_requirements_parse" or "team_guides_retrieve" or "parent_planning_team_state" or "parent_planning_team_save" or "parent_planning_team_delete" or "parent_planning_team_inventory_catalog" or "parent_planning_team_inventory_get" or "parent_planning_team_inventory_save" or "parent_planning_ai_team_propose" or "parent_planning_ai_team_member_seed" => "desktop.parent_planning_api",
        "parent_planning_state" or "parent_planning_account_save" or "parent_planning_bind" or "parent_planning_save" or "parent_planning_delete" or "parent_planning_suggest" or "parent_planning_evaluate" or "parent_planning_route_catalog" or "parent_planning_route_evaluate" or "parent_planning_race_catalog" or "parent_planning_races_get" or "parent_planning_races_save" or "parent_planning_ai_status" or "parent_planning_ai_propose" => "desktop.parent_planning_api",
        _ => throw new InvalidOperationException("不支持的本地规划操作。")
    };
    private static async Task<JObject> Send(JObject payload)
    {
        var module=ModuleFor((string?)payload["action"]);
        var result=await UmaPublicRuntime.Request(payload,module);
        return result[0] as JObject??throw new InvalidOperationException("本地规划结果不完整。");
    }
}
