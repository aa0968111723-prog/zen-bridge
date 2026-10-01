import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "禪譯 Zen Bridge｜社課翻譯工作台",
  description: "淡江禪學社的中英雙向翻譯、講者資料與翻譯筆記。",
  icons: {
    icon: "/art/turtle-favicon.png",
    shortcut: "/art/turtle-favicon.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-Hant">
      <body className="antialiased">{children}</body>
    </html>
  );
}
