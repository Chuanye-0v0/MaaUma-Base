using System;
using System.Collections.Generic;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform;

namespace MFAAvalonia.Helper;

// MaaUma Pacer getting started view v2 (interactive filterable onboarding + V2 workflow/strategy pack guide)
public sealed class MaaUmaPacerGettingStartedView : UserControl
{
    private static readonly Dictionary<string, Bitmap> Images = new();
    private readonly record struct Mark(double X, double Y, double Width, double Height, string Number);

    private static readonly IBrush PageBg = new SolidColorBrush(Color.Parse("#F3F6FB"));
    private static readonly IBrush CardBg = Brushes.White;
    private static readonly IBrush CardBorder = new SolidColorBrush(Color.Parse("#E2E8F0"));
    private static readonly IBrush SoftBlueBg = new SolidColorBrush(Color.Parse("#EFF6FF"));
    private static readonly IBrush SoftBlueBorder = new SolidColorBrush(Color.Parse("#BFDBFE"));
    private static readonly IBrush SoftGreenBg = new SolidColorBrush(Color.Parse("#ECFDF5"));
    private static readonly IBrush SoftGreenBorder = new SolidColorBrush(Color.Parse("#A7F3D0"));
    private static readonly IBrush SoftAmberBg = new SolidColorBrush(Color.Parse("#FFFBEB"));
    private static readonly IBrush SoftAmberBorder = new SolidColorBrush(Color.Parse("#FDE68A"));
    private static readonly IBrush SubCardBg = new SolidColorBrush(Color.Parse("#F8FAFC"));
    private static readonly IBrush InkPrimary = new SolidColorBrush(Color.Parse("#1E293B"));
    private static readonly IBrush InkSecondary = new SolidColorBrush(Color.Parse("#475569"));
    private static readonly IBrush AccentBrush = new SolidColorBrush(Color.Parse("#2563EB"));
    private static readonly IBrush ActivePillBorder = new SolidColorBrush(Color.Parse("#1D4ED8"));

    public MaaUmaPacerGettingStartedView(Action? openOverview = null)
    {
        Name = "PacerGettingStarted";
        var guide = new StackPanel
        {
            Spacing = 18,
            Margin = new Thickness(24),
            MaxWidth = 1080,
            HorizontalAlignment = HorizontalAlignment.Stretch,
        };

        // Top hero header
        var heading = new DockPanel { LastChildFill = true };
        if (openOverview != null)
        {
            var goBtn = new Button
            {
                Name = "PacerGuideOpenOverview",
                Content = "去运行总览开始",
                HorizontalAlignment = HorizontalAlignment.Right,
                Padding = new Thickness(18, 9),
                FontWeight = FontWeight.SemiBold,
                Background = AccentBrush,
                Foreground = Brushes.White,
                CornerRadius = new CornerRadius(8),
            };
            goBtn.Click += (_, _) => openOverview();
            DockPanel.SetDock(goBtn, Dock.Right);
            heading.Children.Add(goBtn);
        }

        var titleStack = new StackPanel { Spacing = 6 };
        titleStack.Children.Add(Text("育成中心新手教程", 26, true));
        titleStack.Children.Add(MutedText("3 分钟完成检查、连模拟器与策略 2.3 首局开跑 · 无需背参数", 14));
        heading.Children.Add(titleStack);

        guide.Children.Add(new Border
        {
            Background = CardBg,
            BorderBrush = CardBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(14),
            Padding = new Thickness(22),
            Child = new StackPanel
            {
                Spacing = 12,
                Children =
                {
                    heading,
                    Callout(
                        SoftBlueBg,
                        SoftBlueBorder,
                        "核心顺序记住一句就够：游戏停在凯旋门育成主页 → 左侧点进「育成配置」选培养方案与模式、选用推荐策略点右上「保存方案」 → 回「运行总览」点底部「按当前配置开始」（不用自己先点游戏里的“训练”）。"),
                },
            },
        });

        // Interactive filter pills
        var sections = new List<(string Key, Control Section)>();
        var pills = new List<(string Key, Button Pill)>();
        string activeKey = "All";

        var filterBar = new WrapPanel
        {
            Orientation = Orientation.Horizontal,
            Margin = new Thickness(0, 2, 0, 2),
        };

        void ApplyFilter(string selectedKey)
        {
            activeKey = selectedKey;
            foreach (var (key, pill) in pills)
            {
                var isActive = string.Equals(key, activeKey, StringComparison.Ordinal);
                pill.Background = isActive ? AccentBrush : CardBg;
                pill.Foreground = isActive ? Brushes.White : InkPrimary;
                pill.BorderBrush = isActive ? ActivePillBorder : CardBorder;
            }

            foreach (var (key, section) in sections)
            {
                section.IsVisible = activeKey == "All" || string.Equals(key, activeKey, StringComparison.Ordinal);
            }
        }

        void AddFilterPill(string key, string label)
        {
            var btn = new Button
            {
                Name = "PacerGuideFilter_" + key,
                Content = label,
                Margin = new Thickness(0, 0, 8, 8),
                Padding = new Thickness(14, 7),
                CornerRadius = new CornerRadius(999),
                BorderThickness = new Thickness(1),
                FontSize = 13,
                FontWeight = FontWeight.SemiBold,
            };
            btn.Click += (_, _) => ApplyFilter(key);
            pills.Add((key, btn));
            filterBar.Children.Add(btn);
        }

        AddFilterPill("All", "全部内容");
        AddFilterPill("Setup", "① 4步极速开跑");
        AddFilterPill("Modes", "② 5种模式区别");
        AddFilterPill("Config", "③ 配置与策略");
        AddFilterPill("Skills", "④ 手动学技能图示");
        AddFilterPill("Faq", "⑤ 高频排障FAQ");
        guide.Children.Add(filterBar);

        void AddSection(string key, Control section)
        {
            sections.Add((key, section));
            guide.Children.Add(section);
        }

        // Section 1: 4-step quickstart
        var setupBody = new StackPanel { Spacing = 12 };
        setupBody.Children.Add(StepCard(
            "第 1 步：确认开源版与可选外部策略包（无需激活）",
            "开源基础版已内置 L'Arc 基线策略（2.0 ~ 2.3），启动即用、无需填写授权码或联网验证；如有自备的外部策略扩展包，放入 pacer/strategy_pack/ 后重开界面即可在策略下拉框自动识别。"));
        setupBody.Children.Add(StepCard(
            "第 2 步：准备并连接模拟器",
            "• 模拟器内部分辨率必须设为竖屏 720×1280（桌面窗口可任意缩放，Windows 缩放/DPI 保持默认即可）。\n" +
            "• 权限必须一致：若 MuMu 等模拟器是以管理员身份启动的，请务必右键「以管理员身份运行」打开本软件，否则点刷新设备会扫不到任何模拟器。\n" +
            "• 到「系统设置 → 模拟器连接」选中正在运行的模拟器，再回「运行总览」核对顶部「共用设备」。日活与育成共用同一连接，同一设备同一时间只运行其中一项。"));
        setupBody.Children.Add(StepCard(
            "第 3 步：选「培养方案」与「育成模式」，选用推荐默认策略",
            "新版界面顶部已升级为「先选培养方案 → 再选育成模式」：\n" +
            "1）点左侧「育成配置」，依次检查 ① 马娘与卡组、② 策略与五维目标、③ 育成模式与条件、④ 赛程 / 技能 / 事件。\n" +
            "2）新手直接在策略下拉选择带「· 默认」的推荐策略（开源基线为 2.3；若装有外部扩展包会自动带出），保持右上绿色「● 官方纯净」即可；不启用 AI 时无需填写 API 密钥；需要末尾自动学技结算请保留「末尾自动学习技能并正常结算」。\n" +
            "3）改完后务必点顶栏右上角「保存方案」，新设置才会在下一次开跑生效。"));
        setupBody.Children.Add(StepCard(
            "第 4 步：摆好游戏画面，开跑或续跑",
            "• 已有未完成局：让游戏停在凯旋门育成主界面（初级出道前的“?”问号回合也可直接接手，不必手动打完新人战），在顶栏选「自动育成」，然后点「运行总览」底部的「按当前配置开始」。\n" +
            "• 主页没有育成局：在游戏中选好种马、在 ① 配好目标角色（含衣装）与借卡，同样点「按当前配置开始」就会自动从主页开局。\n" +
            "• 暂停后继续：优先用底部「继续当前局 / 继续原工作流」，不要误开新局；停止或暂停时若要手工操作游戏，请等界面明确显示「已暂停 / 已停止」后再点模拟器。"));
        AddSection("Setup", Card("① 首次使用 4 步极速开跑", setupBody));

        // Section 2: 5 modes
        var modesBody = new StackPanel { Spacing = 12 };
        modesBody.Children.Add(Callout(
            SoftGreenBg,
            SoftGreenBorder,
            "Option A 方案 × 模式独立记忆：同一套「培养方案」（马娘、配卡、策略、五维、赛程、技能、事件）可在 5 种模式间一键切换；每种模式的筛选门槛、连开局数与 TP/萝卜预算都会独立记忆，刷切消耗设置绝不会串进普通「自动育成」。"));
        modesBody.Children.Add(ModeRow(
            "自动育成",
            "适合日常接管当前局或单开/连开养完",
            "接管已有凯旋门育成，或在游戏主页没有未完成局时按当前方案开新局并养完。默认不花宝石买 TP 药。"));
        modesBody.Children.Add(ModeRow(
            "出切先停，确认后同局养完",
            "适合半自动蹲切后继续养完同一局",
            "先按 ③ 设定的筛选条件与 TP/萝卜预算跑前期，出切后自动停机等你验收；在运行总览点确认继续，就会沿用同一套培养方案接着把这一局养完。"));
        modesBody.Children.Add(ModeRow(
            "刷切并保留",
            "适合批量刷开局切位并保留极品开局",
            "循环重开筛选“才能出众”，默认找到后立刻保留该局并停机，供后续手动或切入自动育成接手；未命中则按截至回合与次数预算重开。"));
        modesBody.Children.Add(ModeRow(
            "SS 筛局循环",
            "适合第一年末五人 SS 强开局筛选",
            "按 ③ 设置的第一年末指定次数五人 SS 门槛自动开局验货：未达标自动放弃重开，达标后再继续完成该局或按连开上限结束。"));
        modesBody.Children.Add(ModeRow(
            "大赛成品马连开循环",
            "适合设定毕业五维与距离后批量连开",
            "在 ③ 设置多局毕业验收条件与目标成品数后连续开跑，每局结束自动验货计入合格数，直到达到目标成品数或耗尽轮次预算。"));
        modesBody.Children.Add(MutedText(
            "切换铁律：任务正在运行中时不能直接切换育成模式；必须先点底部「停止脚本」，等状态明确变成已停止后再换模式。",
            13));
        AddSection("Modes", Card("② 5 种育成模式一表看懂", modesBody));

        // Section 3: Config tabs & Strategy Pack
        var cfgBody = new StackPanel { Spacing = 10 };
        cfgBody.Children.Add(SectionExpander(
            "① 马娘与卡组：接管局选「沿用当前角色」，开新局再选具体马娘",
            "• 如果游戏里已经开了一半，在 ① 勾选/选择「沿用当前角色」（保持现有角色与配卡）；运行中绝对不会在局内换马或换卡。\n" +
            "• 如果从游戏主页面新开一局，务必准确选择角色及对应衣装，并核对借卡等级/满破与支援卡类型，避免因「角色与配置不一致」停机。"));
        cfgBody.Children.Add(SectionExpander(
            "② 策略与五维目标：策略选择 + 纯度检测徽章",
            "• 开源基础版默认提供 2.3 基线策略（内置 15 维评分权重与 28% 默认失败率上限）；若安装了外部策略扩展包，下拉框会动态读取 manifest 列出扩展版本。\n" +
            "• 修改五维目标（如 1550/1250/1300/1100/1300）不会破坏纯净状态；只有改到高级权重/隐藏覆盖时，顶栏徽章才会变为「已自定义」。想随时回到标准参数，点「✦ 一键恢复官方纯净」即可——它永远不会清空你设好的模式门槛、连开局数与萝卜预算。"));
        cfgBody.Children.Add(SectionExpander(
            "③ 育成模式与条件 & 日期停点：默认停点 vs 本局暂停计划",
            "• 在 ③ 里配置刷切截至回合、TP 药 / 萝卜预算与随培养方案保存的默认停点（比如「首次凯旋门前停下，手动学技能」「末次凯旋门前交还操作」）。\n" +
            "• 开跑后如果想临时在这局多停一次，不用回配置页面改全局：直接在「运行总览 → 本局暂停计划」增删停点，并点击「应用本局暂停计划」，会在后续检查点立即生效；注意删除停点不会自动解除已经触发的当前暂停。"));
        cfgBody.Children.Add(SectionExpander(
            "④ 赛程 / 技能 / 事件：常用微调不踩坑",
            "• 可指定事件 With 优先支援卡（例如 里见光钻 → 神鹰）、目标技能/忽略技能、借用切牌与自定义比赛赛程。\n" +
            "• 初次使用只要留好「末尾自动学习技能并正常结算」与常用目标技能，不必把整张表全填满。"));
        AddSection("Config", Card("③ 「育成配置」①②③④ 与策略避坑指南", cfgBody));

        // Section 4: Manual skill purchase + annotated pictures
        var skillsBody = new StackPanel { Spacing = 12 };
        skillsBody.Children.Add(Paragraphs(
            "只有开启了「首次凯旋门前停下，手动学技能」或中途人工接管时才需要亲自进游戏点技能，其余情况全自动运行：",
            "1. 先看面板状态变成「已暂停 / 等待人工继续」再动手，不要在自动操作中强点模拟器；技能入口在普通育成主页右下角，比赛日布局在左下角（见下方图 A / 图 B）。",
            "2. 点右侧绿色「+」勾选想学的技能，再点底部「确定」（未选技能时灰色不可点属正常现象，不用照抄示例图技能）；弹出购买确认框就点确认。",
            "3. 买完技能后，务必点游戏左下角「返回」，回到凯旋门育成主页面。",
            "4. 回到 Maa 面板：若卡在技能交接停点，点「技能已学完，继续本局」；普通暂停点「继续当前局」。"));
        skillsBody.Children.Add(Pictures());
        AddSection("Skills", Card("④ 手动学技能：4 步口诀 + 原生游戏图示", skillsBody));

        // Section 5: FAQ & diagnostics
        var faqBody = new StackPanel { Spacing = 10 };
        faqBody.Children.Add(SectionExpander(
            "Q1：点刷新搜不到模拟器 / 原本连着的 1 号刷一下也没了",
            "几乎 100% 是窗口权限不匹配导致：MuMu 模拟器如果是管理员权限运行，而 Maa 是普通权限启动，Windows 会拦截进程扫描。解决：完全关闭面板，右键快捷方式选「以管理员身份运行」（建议在快捷方式属性 → 兼容性勾选一直以管理员运行），重新打开再点刷新；同时确认模拟器为 720×1280 竖屏。"));
        faqBody.Children.Add(SectionExpander(
            "Q2：如何安装或切换外部策略扩展包？",
            "开源基础版无需授权即可直接使用内置 2.0 ~ 2.3 基线策略。如果你有自备的策略扩展包，将其解压放入 pacer/strategy_pack/（包含 manifest.json），重启界面即可在策略下拉列表看到并切换，本局进度不会丢失。"));
        faqBody.Children.Add(SectionExpander(
            "Q3：停在「画面尚未识别」/「海外适应性读取未完成」或迟迟不推进超时停机",
            "1）先看模拟器画面是不是刚点错了游戏自带的「自主训练中（倒计时）」——这个页面不在凯旋门自动识别范围内，点取消退回育成主页或主界面即可。\n" +
            "2）如果是网络加载圈、临时弹窗或过场淡入导致未识别，本局也会自动安全保留，先清掉弹窗回到育成主页再点「继续当前局」即可；若页面稳定却仍停机，可直接在旁边的「查看并提交停止报告」上传留档给维护者排查。"));
        faqBody.Children.Add(SectionExpander(
            "Q4：提示「角色与配置不一致」",
            "说明游戏里当前育成的马娘/衣装和 ①「马娘与卡组」选的不一样。接管中途局请在 ① 改为「沿用当前角色」，或者核对角色与衣装后再继续。"));
        faqBody.Children.Add(SectionExpander(
            "Q5：提示「同设备日活运行中 / 请先停止原任务」",
            "同一个模拟器同时只能跑日活或育成其中一件事。先去把正在占用那台模拟器的日活任务或旧育成停掉，等状态变成闲置再点开始。"));
        faqBody.Children.Add(SectionExpander(
            "Q6：提示 TP 不足停机怎么办？",
            "「自动育成」默认严禁花宝石/自动吃药，TP 不够时会安全停机等你手动补充或切设置；如果跑刷切或连开循环并允许补体力，请到 ③「育成模式与条件」里把允许消耗的 TP 药或萝卜预算调高再保存。"));
        faqBody.Children.Add(SectionExpander(
            "Q7：遇到疑难停机怎么反馈？（停机报告 vs 导出本机诊断 ZIP）",
            "• 「查看并提交停止报告」：弹窗内可先预览停机前留档的游戏图与脱敏摘要，确认无误再私密提交，成功会给出 BF 反馈编号；可自行取消勾选附图，绝不包含账号或凭据。\n" +
            "• 识别失败自动留档：发生识别异常时先在本地留档，默认仅在整局真正结束后私密上传给维护者（可在「系统设置 → 常规」关闭自动上传）。\n" +
            "• 「导出本次诊断」：只打包白名单内的失败流水线、错误JSON与脱敏日志为本机 ZIP，不自动上传、不连模拟器也不暂停当前任务，方便你自查或直接发给维护者。"));
        AddSection("Faq", Card("⑤ 新手高频 7 大卡点 FAQ 与反馈诊断说明", faqBody));

        ApplyFilter("All");

        Content = new ScrollViewer
        {
            Background = PageBg,
            HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,
            Content = guide,
        };
    }

    private static Control Pictures()
    {
        var pictures = new WrapPanel { Orientation = Orientation.Horizontal };
        pictures.Children.Add(Picture("career-home.png", "A. 凯旋门育成主页",
            "先在 Maa 点「按当前配置开始」。标号 1 是右下角技能入口；只有手动停点学技才需点进，平时全自动不用碰。",
            new Mark(509, 934, 186, 121, "1")));
        pictures.Children.Add(Picture("race-day-skill-entry.png", "B. 比赛日技能入口",
            "比赛日界面时技能入口（标号 1）在左下角。先等面板显示已暂停再进游戏点击。",
            new Mark(26, 1025, 188, 120, "1")));
        pictures.Children.Add(Picture("skill-selection.png", "C. 选技能 → 确定 → 返回",
            "1 看剩余 Pt；2 点绿色 + 选技能；3 选完点确定（未选时灰色正常）；4 点返回回育成主页，再回 Maa 点继续。",
            new Mark(273, 396, 417, 52, "1"), new Mark(626, 836, 52, 53, "2"),
            new Mark(216, 1040, 288, 83, "3"), new Mark(22, 1190, 125, 85, "4")));
        return pictures;
    }

    private static Border Picture(string file, string title, string explanation, params Mark[] marks)
    {
        var content = new StackPanel { Spacing = 9 };
        content.Children.Add(Text(title, 15, true));
        try
        {
            var image = AnnotatedImage(file, marks);
            var button = new Button
            {
                Name = "PacerGuideImage_" + file.Replace('-', '_').Replace('.', '_'),
                Padding = new Thickness(0),
                Background = Brushes.Transparent,
                BorderThickness = new Thickness(0),
                HorizontalAlignment = HorizontalAlignment.Center,
                Content = new Viewbox { Width = 260, Stretch = Stretch.Uniform, Child = image },
            };
            ToolTip.SetTip(button, "点击查看原生大小图示");
            button.Click += (_, _) =>
            {
                // Each modal builds fresh visual elements because the source bitmap is shared.
                var zoom = new Window
                {
                    Title = title + " · 按钮位置图示",
                    Width = 780,
                    Height = 920,
                    WindowStartupLocation = WindowStartupLocation.CenterOwner,
                    Content = new ScrollViewer
                    {
                        Content = AnnotatedImage(file, marks),
                        HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,
                        VerticalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,
                    },
                };
                if (TopLevel.GetTopLevel(button) is Window owner && owner.IsVisible)
                    zoom.Show(owner);
                else
                    zoom.Show();
            };
            content.Children.Add(button);
            content.Children.Add(MutedText("点图可放大查看", 12));
        }
        catch
        {
            content.Children.Add(Text("图示暂未读取，可继续按上方文字步骤操作。", 13));
        }
        content.Children.Add(MutedText(explanation, 13));
        return new Border
        {
            Width = 294,
            Margin = new Thickness(0, 0, 12, 12),
            Padding = new Thickness(12),
            Background = SubCardBg,
            BorderBrush = CardBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(12),
            Child = content,
        };
    }

    private static Canvas AnnotatedImage(string file, Mark[] marks)
    {
        var canvas = new Canvas { Width = 720, Height = 1280, ClipToBounds = true };
        Bitmap? bitmap = null;
        if (!Images.TryGetValue(file, out bitmap))
        {
            try
            {
                var uri = new Uri("avares://MFAAvalonia.Core/Assets/MaaUmaPacerGuide/" + file);
                if (AssetLoader.Exists(uri))
                {
                    bitmap = new Bitmap(AssetLoader.Open(uri));
                    Images.Add(file, bitmap);
                }
            }
            catch
            {
                bitmap = null;
            }
        }
        if (bitmap != null)
        {
            // Tutorial-only overlay boxes scale visually with the canvas; they never reach an executor.
            canvas.Children.Add(new Image
            {
                Name = "PacerGuideBitmap_" + file.Replace('-', '_').Replace('.', '_'),
                Source = bitmap,
                Width = 720,
                Height = 1280,
                Stretch = Stretch.Fill,
            });
        }
        else
        {
            var placeholder = new Border
            {
                Width = 720,
                Height = 1280,
                Background = SubCardBg,
                BorderBrush = CardBorder,
                BorderThickness = new Thickness(2),
                CornerRadius = new CornerRadius(12),
                Padding = new Thickness(48),
                Child = new TextBlock
                {
                    Text = "图示未包含在纯源码仓库，请参阅文字说明或发行包资源。",
                    FontSize = 24,
                    Foreground = InkSecondary,
                    TextWrapping = TextWrapping.Wrap,
                    HorizontalAlignment = HorizontalAlignment.Center,
                    VerticalAlignment = VerticalAlignment.Center,
                    TextAlignment = TextAlignment.Center,
                },
            };
            canvas.Children.Add(placeholder);
        }
        foreach (var mark in marks)
        {
            var outline = new Border
            {
                Width = mark.Width,
                Height = mark.Height,
                BorderThickness = new Thickness(6),
                BorderBrush = new SolidColorBrush(Color.Parse("#E3344F")),
                CornerRadius = new CornerRadius(9),
                IsHitTestVisible = false,
            };
            Canvas.SetLeft(outline, mark.X);
            Canvas.SetTop(outline, mark.Y);
            canvas.Children.Add(outline);

            var badge = new Border
            {
                Width = 52,
                Height = 52,
                CornerRadius = new CornerRadius(26),
                Background = new SolidColorBrush(Color.Parse("#D82441")),
                IsHitTestVisible = false,
                Child = new TextBlock
                {
                    Text = mark.Number,
                    FontSize = 36,
                    FontWeight = FontWeight.Bold,
                    Foreground = Brushes.White,
                    HorizontalAlignment = HorizontalAlignment.Center,
                    VerticalAlignment = VerticalAlignment.Center,
                },
            };
            Canvas.SetLeft(badge, mark.X);
            Canvas.SetTop(badge, Math.Max(2, mark.Y - 58));
            canvas.Children.Add(badge);
        }
        return canvas;
    }

    private static Border Callout(IBrush bg, IBrush border, string message) => new()
    {
        Background = bg,
        BorderBrush = border,
        BorderThickness = new Thickness(1),
        CornerRadius = new CornerRadius(10),
        Padding = new Thickness(14, 10),
        Child = Text(message, 13.5),
    };

    private static Border StepCard(string title, string body) => new()
    {
        Background = SubCardBg,
        BorderBrush = CardBorder,
        BorderThickness = new Thickness(1),
        CornerRadius = new CornerRadius(10),
        Padding = new Thickness(14, 12),
        Child = new StackPanel
        {
            Spacing = 6,
            Children =
            {
                Text(title, 15.5, true),
                Text(body, 13.5),
            },
        },
    };

    private static Border ModeRow(string name, string badgeText, string description)
    {
        var header = new WrapPanel { Orientation = Orientation.Horizontal };
        header.Children.Add(new TextBlock
        {
            Text = name,
            FontSize = 15.5,
            FontWeight = FontWeight.SemiBold,
            Foreground = InkPrimary,
            Margin = new Thickness(0, 0, 10, 4),
            VerticalAlignment = VerticalAlignment.Center,
        });
        header.Children.Add(new Border
        {
            Background = SoftBlueBg,
            BorderBrush = SoftBlueBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(10, 2),
            Child = new TextBlock
            {
                Text = badgeText,
                FontSize = 12,
                Foreground = AccentBrush,
                FontWeight = FontWeight.SemiBold,
            },
        });
        return new Border
        {
            Background = SubCardBg,
            BorderBrush = CardBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Padding = new Thickness(14, 10),
            Child = new StackPanel
            {
                Spacing = 5,
                Children = { header, Text(description, 13.5) },
            },
        };
    }

    private static Expander SectionExpander(string header, string body) => new()
    {
        Header = Text(header, 14.5, true),
        IsExpanded = false,
        HorizontalAlignment = HorizontalAlignment.Stretch,
        Content = new Border
        {
            Background = SubCardBg,
            BorderBrush = CardBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(12, 10),
            Margin = new Thickness(0, 4, 0, 0),
            Child = Text(body, 13.5),
        },
    };

    private static TextBlock Text(string text, double size, bool bold = false) => new()
    {
        Text = text,
        FontSize = size,
        TextWrapping = TextWrapping.Wrap,
        Foreground = InkPrimary,
        FontWeight = bold ? FontWeight.SemiBold : FontWeight.Normal,
        LineHeight = size * 1.62,
    };

    private static TextBlock MutedText(string text, double size) => new()
    {
        Text = text,
        FontSize = size,
        TextWrapping = TextWrapping.Wrap,
        Foreground = InkSecondary,
        LineHeight = size * 1.55,
    };

    private static StackPanel Paragraphs(params string[] text)
    {
        var panel = new StackPanel { Spacing = 8 };
        foreach (var paragraph in text) panel.Children.Add(Text(paragraph, 13.5));
        return panel;
    }

    private static Border Card(string title, Control body) => new()
    {
        Background = CardBg,
        BorderBrush = CardBorder,
        BorderThickness = new Thickness(1),
        CornerRadius = new CornerRadius(12),
        Padding = new Thickness(20),
        Child = new StackPanel { Spacing = 12, Children = { Text(title, 18, true), body } },
    };
}
