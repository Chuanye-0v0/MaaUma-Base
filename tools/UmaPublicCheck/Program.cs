using System.Reflection;
using System.Runtime.Loader;
var root = AppContext.BaseDirectory;
AssemblyLoadContext.Default.Resolving += (_,name) => {
    foreach(var folder in new[]{root,Path.Combine(root,"libs")}) {
        var path=Path.Combine(folder,name.Name+".dll");
        if(File.Exists(path))return AssemblyLoadContext.Default.LoadFromAssemblyPath(path);
    }
    return null;
};
var assembly=AssemblyLoadContext.Default.LoadFromAssemblyPath(Path.Combine(root,"libs","MFAAvalonia.Core.dll"));
var type=assembly.GetType("MFAAvalonia.Helper.UmaPublicRuntime",throwOnError:true)!;
return (int)type.GetMethod("SelfTest",BindingFlags.Static|BindingFlags.Public)!.Invoke(null,null)!;
