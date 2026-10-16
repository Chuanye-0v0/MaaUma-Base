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
    private static async Task<JObject> Send(JObject payload)
    {
        if((string?)payload["action"] is not ("parent_planner_catalog" or "parent_planner_evaluate"))
            throw new InvalidOperationException("不支持的本地规划操作。");
        var result=await UmaPublicRuntime.Request(payload,"desktop.parent_planner_api");
        return result[0] as JObject??throw new InvalidOperationException("本地规划结果不完整。");
    }
}
