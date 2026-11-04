// "打开文件夹"弹窗：浏览器拿不到本地绝对路径，所以由本地服务列目录（/api/fs），这里逐层点进去选择。
import { useEffect, useState } from "react";
import { ArrowUp, Check, ChevronRight, Clock, Folder, FolderGit2, Home } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { listDirs, type DirListing } from "@/lib/agent";
import { cn } from "@/lib/utils";

export function WorkspacePicker({ open, onOpenChange, current, recent, onPick }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  current: string;
  recent: string[];
  onPick: (path: string) => void;
}) {
  const [listing, setListing] = useState<DirListing | null>(null);
  const [typed, setTyped] = useState("");

  const go = async (path: string) => {
    const next = await listDirs(path);
    if (next.error) return;
    setListing(next);
    setTyped(next.path);
  };

  useEffect(() => {
    if (open) go(current);
  }, [open, current]);

  const pick = (path: string) => {
    onPick(path);
    onOpenChange(false);
  };

  const crumbs = listing ? listing.path.split("/").filter(Boolean) : [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="glass-panel gap-0 overflow-hidden p-0 sm:max-w-2xl">
        <DialogHeader className="border-white/10 border-b px-5 pt-5 pb-4">
          <DialogTitle className="flex items-center gap-2">
            <FolderGit2 className="size-5 text-violet-400" /> 打开工作区
          </DialogTitle>
          <DialogDescription>选择一个文件夹，agent 会在里面读写代码、运行命令。</DialogDescription>
        </DialogHeader>

        <div className="grid min-h-[22rem] grid-cols-[11rem_1fr] max-sm:grid-cols-1">
          {/* 最近使用 */}
          <div className="border-white/10 border-r p-3 max-sm:hidden">
            <div className="mb-2 flex items-center gap-1.5 px-2 text-muted-foreground text-xs"><Clock className="size-3.5" /> 最近</div>
            {recent.length === 0 && <p className="px-2 text-muted-foreground text-xs">还没有</p>}
            {recent.map((path) => (
              <button key={path} onClick={() => pick(path)} title={path}
                className="w-full truncate rounded-md px-2 py-1.5 text-left text-sm hover:bg-white/5">
                {path.split("/").filter(Boolean).pop()}
              </button>
            ))}
          </div>

          {/* 目录浏览 */}
          <div className="flex min-w-0 flex-col">
            <div className="flex items-center gap-1 border-white/10 border-b px-3 py-2">
              <Button variant="ghost" size="icon" className="size-7" title="主目录" onClick={() => go("")}><Home className="size-4" /></Button>
              <Button variant="ghost" size="icon" className="size-7" title="上一级" disabled={!listing?.parent}
                onClick={() => listing?.parent && go(listing.parent)}><ArrowUp className="size-4" /></Button>
              {/* 只显示最后 3 级，前面折成"…"，避免长路径撑出滚动条 */}
              <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-hidden text-muted-foreground text-xs">
                {crumbs.length > 3 && (
                  <button className="shrink-0 rounded px-1 py-0.5 hover:bg-white/5 hover:text-foreground" title={listing?.path}
                    onClick={() => go("/" + crumbs.slice(0, crumbs.length - 3).join("/"))}>…</button>
                )}
                {crumbs.map((part, i) => i < crumbs.length - 3 ? null : (
                  <button key={i} className="flex min-w-0 items-center gap-0.5 rounded px-1 py-0.5 hover:bg-white/5 hover:text-foreground"
                    onClick={() => go("/" + crumbs.slice(0, i + 1).join("/"))}>
                    {(i > 0) && <ChevronRight className="size-3 shrink-0 opacity-50" />}<span className="truncate">{part}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="max-h-[18rem] min-h-0 flex-1 overflow-y-auto p-2">
              {listing?.dirs.length === 0 && <p className="p-3 text-muted-foreground text-sm">这里没有子文件夹</p>}
              {listing?.dirs.map((dir) => (
                <button key={dir.name} onDoubleClick={() => pick(`${listing.path}/${dir.name}`)}
                  onClick={() => go(`${listing.path}/${dir.name}`)}
                  className="group flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-white/5">
                  {dir.isRepo ? <FolderGit2 className="size-4 text-violet-400" /> : <Folder className="size-4 text-muted-foreground" />}
                  <span className="truncate">{dir.name}</span>
                  {dir.isRepo && <span className="ml-auto rounded-full bg-violet-500/15 px-1.5 text-[10px] text-violet-300">git</span>}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2 border-white/10 border-t p-3">
              <Input className="h-9 flex-1 font-mono text-xs" value={typed} spellCheck={false}
                onChange={(e) => setTyped(e.target.value)} onKeyDown={(e) => e.key === "Enter" && go(typed)} />
              <Button className={cn("gap-1.5 bg-gradient-to-r from-violet-500 to-cyan-500 text-white hover:opacity-90")}
                disabled={!listing} onClick={() => listing && pick(listing.path)}>
                <Check className="size-4" /> 选择此文件夹
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
