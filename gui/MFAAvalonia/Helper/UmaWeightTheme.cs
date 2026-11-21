using System;
using System.Collections.Generic;
using Avalonia.Controls;
using Avalonia.Media;
using Avalonia.Styling;

namespace MFAAvalonia.Helper;

/// <summary>Per-control brushes follow the containing panel, including live theme changes.</summary>
public static class UmaWeightTheme
{
    private static readonly IReadOnlyDictionary<string, (string Light, string Dark)> Colors = new Dictionary<string, (string, string)>
    {
        ["canvas"] = ("#F5F7FC", "#17191F"), ["panel"] = ("#FFFFFF", "#22262F"),
        ["card"] = ("#FFFFFF", "#252B35"), ["border"] = ("#AFBDD0", "#353B48"),
        ["text"] = ("#24334B", "#E2E6EF"), ["muted"] = ("#52627B", "#9AA4B7"),
        ["reference"] = ("#EBF1FB", "#19212B"), ["selected"] = ("#9B630C", "#F0CE80"),
        ["barTrack"] = ("#E8EDF6", "#151B24"), ["barAxis"] = ("#7D8CA3", "#637187"),
        ["positive"] = ("#21755D", "#70B7A5"), ["negative"] = ("#B33B4F", "#CB8187"),
        ["grid"] = ("#D3DCEB", "#343B46"), ["nodeBorder"] = ("#9AAAC1", "#101216"),
        ["entry"] = ("#EDF2FA", "#303846"), ["headerText"] = ("#14223A", "#11151E"),
        ["wireSelected"] = ("#172D50", "#FFFFFF"), ["wirePending"] = ("#986415", "#F1D58D"),
        ["calendarDomestic"] = ("#AFC7E6", "#3B495F"), ["calendarOverseas"] = ("#D9AF70", "#966536"),
        ["calendarBorder"] = ("#EBF0F8", "#202630")
    };

    public static bool IsLight(Control owner) => owner.ActualThemeVariant != ThemeVariant.Dark;
    public static Color Color(Control owner, string role)
    {
        if (!Colors.TryGetValue(role, out var pair)) throw new ArgumentException("Unknown weight-editor color role: " + role);
        return Avalonia.Media.Color.Parse(IsLight(owner) ? pair.Light : pair.Dark);
    }
    public static IBrush Brush(Control owner, string role)
    {
        var brush = new SolidColorBrush(Color(owner, role));
        void Refresh() { brush.Color = Color(owner, role); owner.InvalidateVisual(); }
        owner.ActualThemeVariantChanged += (_, _) => Refresh();
        owner.AttachedToVisualTree += (_, _) => Refresh();
        return brush;
    }
    public static IBrush Category(Control owner, string type)
    {
        string Hex(bool light) => type switch
        {
            "start" => light ? "#45658E" : "#7188B4", "stage" or "stage_flow" => light ? "#705099" : "#A78BD2",
            "condition" => light ? "#28796E" : "#56C2B4", "priority" => light ? "#95620A" : "#E5AE58",
            "weights" => light ? "#96529C" : "#D38FD6", "attributes" or "score_attributes" => light ? "#28734B" : "#77C99A",
            "score_bond" => light ? "#966015" : "#E3B86E", "score_charge" => light ? "#376C9D" : "#7CB6EC",
            "score_recovery" => light ? "#28776D" : "#79C9BA", "score_stage" => light ? "#70549F" : "#AB9AE5",
            "score_risk" => light ? "#A34B50" : "#E49393", "adaptation" => light ? "#996032" : "#D69A66",
            "rule" or "rule_flow" => light ? "#3C6F9F" : "#83ADE4", _ => light ? "#5F6F87" : "#9CA8BC"
        };
        var brush = new SolidColorBrush(Avalonia.Media.Color.Parse(Hex(IsLight(owner))));
        void Refresh() { brush.Color = Avalonia.Media.Color.Parse(Hex(IsLight(owner))); owner.InvalidateVisual(); }
        owner.ActualThemeVariantChanged += (_, _) => Refresh();
        owner.AttachedToVisualTree += (_, _) => Refresh();
        return brush;
    }
}
