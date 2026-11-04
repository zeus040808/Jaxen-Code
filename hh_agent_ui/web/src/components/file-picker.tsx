// "从工作区选择文件"弹窗：输入关键字由服务端搜索（/api/files，遵守 .gitignore），可多选。
import { useEffect, useState } from "react";
import { Check, FileCode2 } from "lucide-react";

import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { listFiles } from "@/lib/agent";

export function FilePicker({ open, onOpenChange, cwd, selected, onToggle }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  cwd: string;
  selected: string[];
  onToggle: (path: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<{ files: string[]; total: number }>({ files: [], total: 0 });

  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(async () => {  // 输入停顿 150ms 再搜，别每个字都请求
      const next = await listFiles(cwd, query);
      if (!next.error) setResult(next);
    }, 150);
    return () => clearTimeout(timer);
  }, [open, cwd, query]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="glass-panel overflow-hidden p-0 sm:max-w-xl">
        <DialogHeader className="sr-only">
          <DialogTitle>选择文件</DialogTitle>
          <DialogDescription>选中的文件会作为本轮任务的工作范围</DialogDescription>
        </DialogHeader>
        <Command shouldFilter={false} className="bg-transparent">
          <CommandInput placeholder="搜索工作区文件…（可多选，回车或点击切换）" value={query} onValueChange={setQuery} />
          <CommandList className="max-h-[22rem]">
            <CommandEmpty>没有匹配的文件</CommandEmpty>
            <CommandGroup heading={`${result.total} 个文件${result.total > result.files.length ? `，显示前 ${result.files.length} 个` : ""}`}>
              {result.files.map((path) => {
                const on = selected.includes(path);
                const slash = path.lastIndexOf("/");
                return (
                  <CommandItem key={path} value={path} onSelect={() => onToggle(path)} className="gap-2">
                    <FileCode2 className={on ? "text-violet-400" : "text-muted-foreground"} />
                    <span className="truncate">
                      <span className="text-foreground">{path.slice(slash + 1)}</span>
                      {slash > 0 && <span className="ml-2 text-muted-foreground text-xs">{path.slice(0, slash)}</span>}
                    </span>
                    {on && <Check className="ml-auto text-violet-400" />}
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
