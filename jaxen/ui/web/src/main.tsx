import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./index.css";

// 默认深色；用户在侧边栏切换后记住选择（shadcn 主题用 .dark 类切换）
let theme = "dark";
try { theme = localStorage.getItem("jaxen.theme") || "dark"; } catch { /* 隐私模式 */ }
document.documentElement.classList.toggle("dark", theme === "dark");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TooltipProvider>
      <App />
    </TooltipProvider>
  </StrictMode>,
);
