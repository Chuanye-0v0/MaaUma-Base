using System;
using System.IO;
using Newtonsoft.Json.Linq;
namespace MFAAvalonia.Helper;
internal static class MaaUmaPortableCatalogConfig
{
    internal static JObject Config(string? appDirectory = null)
    {
        var root = Path.GetFullPath(appDirectory ?? AppContext.BaseDirectory);
        var filePath = Path.Combine(root, "local_backend.json");
        var value = File.Exists(filePath)
            ? JObject.Parse(File.ReadAllText(filePath))
            : new JObject
            {
                ["portable"] = true,
                ["python"] = "runtime/python.exe",
                ["repo"] = "backend",
                ["worker"] = "backend/scripts/mfa_worker.py",
                ["skill_database"] = "backend/data/skill_database.json",
                ["tesseract_dir"] = "ocr",
                ["tessdata"] = "ocr/tessdata",
                ["pacer_repo"] = "pacer/backend",
                ["pacer_python"] = "pacer/runtime/python.exe",
                ["endpoint"] = "127.0.0.1:16384",
                ["adb_path"] = "",
            };
        if ((bool?)value["portable"] == true)
            foreach (var key in new[] { "python", "repo", "worker", "pacer_repo", "pacer_python", "adb_path" })
                if (value[key]?.Type == JTokenType.String && (string?)value[key] is string path
                    && !string.IsNullOrWhiteSpace(path) && !Path.IsPathRooted(path))
                    value[key] = Path.GetFullPath(Path.Combine(root, path));
        return value;
    }

}
