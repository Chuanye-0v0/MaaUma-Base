using System;
using Avalonia.Controls;
using Avalonia.Layout;

namespace MFAAvalonia.Helper;

public sealed class UmaParentWorkspacePage : UserControl
{
    public UmaParentWorkspacePage() : this(() => new UmaParentLibraryPage(), () => new UmaParentPlannerPage()) { }

    internal UmaParentWorkspacePage(Func<Control> createLibrary, Func<Control> createPlanner)
    {
        var library = new TabItem { Name = "ParentWorkspaceLibraryTab", Header = "种马库" };
        var planner = new TabItem { Name = "ParentWorkspacePlanningTab", Header = "种马规划" };
        var tabs = new TabControl
        {
            Name = "ParentWorkspaceTabs",
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Stretch,
            ItemsSource = new[] { library, planner },
            SelectedIndex = 0
        };
        var opened = false;
        void LoadSelectedTab()
        {
            if (tabs.SelectedItem == library)
                library.Content ??= createLibrary();
            else if (tabs.SelectedItem == planner)
                planner.Content ??= createPlanner();
        }
        tabs.SelectionChanged += (_, args) =>
        {
            if (opened && ReferenceEquals(args.Source, tabs))
                LoadSelectedTab();
        };
        Loaded += (_, _) =>
        {
            opened = true;
            LoadSelectedTab();
        };
        Content = tabs;
    }
}
