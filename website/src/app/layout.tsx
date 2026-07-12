import type { Metadata } from "next";
import { Hanken_Grotesk } from "next/font/google";
import "./globals.css";
import { SiteHeader } from "@/components/site/SiteHeader";

// Open-font stand-in for writeit.ai's domain-locked proxima-nova. Self-hosted
// by next/font so the site stays a self-contained module.
const hanken = Hanken_Grotesk({
  subsets: ["latin"],
  variable: "--font-hanken",
});

const siteUrl = "https://loopy.writeit.ai";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: "loopy-loop — Documentation",
    template: "%s — loopy-loop",
  },
  description:
    "Run long-running AI agent workflows inside your repository. Documentation for loopy-loop.",
  openGraph: {
    title: "loopy-loop — Documentation",
    description:
      "Run long-running AI agent workflows inside your repository.",
    url: siteUrl,
    siteName: "loopy-loop",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className={`${hanken.variable} font-sans antialiased`}>
        <SiteHeader />
        <main>{children}</main>
      </body>
    </html>
  );
}
