using System;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Read optional draft containers by JSON type, including explicit JSON null.</summary>
internal static class TeamPlanningData
{
    internal static JObject? Plan(JObject team) => team["team_plan"] as JObject;
    internal static JObject[] Members(JObject team) =>
        (Plan(team)?["members"] as JArray ?? new JArray()).OfType<JObject>().ToArray();
    internal static string Summary(JObject team) => TeamPlanningUi.Str(Plan(team)?["summary"]);

    internal static string Failure(JObject response, string fallback)
    {
        var result = (response["results"] as JArray)?.OfType<JObject>().FirstOrDefault();
        var message = TeamPlanningUi.Str(response["message"]);
        if (message.Length == 0) message = TeamPlanningUi.Str(result?["message"]);
        if (message.Length == 0) message = fallback;
        var code = TeamPlanningUi.Str(response["code"]);
        if (code.Length == 0) code = TeamPlanningUi.Str(result?["code"]);
        return code.Length == 0 ? message : message + "\n错误编号：" + code;
    }

    internal static bool IsFailure(JObject response) => response["ok"]?.Type == JTokenType.Boolean &&
        response["ok"]!.Value<bool>() == false;
}
