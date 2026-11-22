import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "INPROOM",
  description: "Intelligent Process Orchestration & Management",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          fontFamily: "Inter, system-ui, sans-serif",
          background: "#F6F8FC",
          color: "#243448",
        }}
      >
        {children}
      </body>
    </html>
  );
}
