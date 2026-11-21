using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia.Controls;
using Avalonia.LogicalTree;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaCareerWorkflowEditor
{
    private readonly HashSet<UmaDecisionPreferences> observedPolicyEditors = new();

    /// <summary>The outer mode/configuration library owns persistence and run commands.</summary>
    public bool EmbeddedLibraryMode
    {
        get => embeddedLibraryMode;
        set {
            embeddedLibraryMode = value;
            RefreshChrome();
            if(value)status.Text = "流程来自当前模式；策略修改作用于所选配置。";
        }
    }

    public bool HideLibraryControls { get => EmbeddedLibraryMode; set => EmbeddedLibraryMode = value; }

    /// <summary>Flush the currently open strategy into this document before switching configurations.</summary>
    public void SavePendingPolicy() => SavePolicy();

    public void ShowFlow() => ClosePolicy();

    /// <summary>Actual execution-node choices for the outer strategy page; never chooses a phase implicitly.</summary>
    public JArray ReadPolicyChoices() => new(Nodes.Where(node => (string?)node["kind"] is "cut" or "career").Select(node => new JObject {
        ["node_id"] = node["id"]?.DeepClone(), ["label"] = StepLabel(node), ["phase"] = (string?)node["kind"] == "cut" ? "前期培养" : "养完阶段",
        ["policy_id"] = (string?)node["policy"] ?? "default", ["summary"] = PolicySummary(node)
    }));

    private void RefreshChrome()
    {
        var strategyOpen = editingPolicy != null || editingFilter != null;
        flowTools.IsVisible = flowActions.IsVisible = !strategyOpen;
        status.IsVisible = !strategyOpen;
        foreach(var control in libraryControls)control.IsVisible = !EmbeddedLibraryMode && !strategyOpen;
        resumeWorkflow.IsVisible = !EmbeddedLibraryMode && !strategyOpen && resumeRoot != null && resumeReady;
    }

    /// <summary>Open the strategy attached to one actual execution node.</summary>
    public bool SelectPolicy(string nodeId) => SelectPolicy(nodeId, showReturn: true);

    internal bool SelectPolicy(string nodeId, bool showReturn)
    {
        if(busy || Node(nodeId) is not JObject node || (string?)node["kind"] is not ("career" or "cut"))return false;
        selected = nodeId; BuildInspector(); OpenPolicy(nodeId);
        back.IsVisible = showReturn;
        return true;
    }

    private void AttachPolicyChanged()
    {
        var pending = new Stack<Control>(); var seen = new HashSet<Control>(); pending.Push(policyEditor);
        while(pending.Count > 0) {
            var control = pending.Pop(); if(!seen.Add(control))continue;
            if(control is UmaDecisionPreferences preferences && observedPolicyEditors.Add(preferences))
                preferences.Changed += PolicyDraftChanged;
            foreach(var child in control.GetLogicalChildren().OfType<Control>())pending.Push(child);
            if(control is Panel panel)foreach(var child in panel.Children)pending.Push(child);
            if(control is Decorator decorator && decorator.Child is Control decorated)pending.Push(decorated);
            if(control is ContentControl contentControl && contentControl.Content is Control contentChild)pending.Push(contentChild);
        }
    }

    private void PolicyDraftChanged()
    {
        if(!loading && !loadingPolicy && !savingPolicy && editingPolicy != null)Changed?.Invoke();
    }
}
