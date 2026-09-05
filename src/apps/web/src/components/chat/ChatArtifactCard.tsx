import type { ComponentProps, ReactNode } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/cn";

export function ChatArtifactCard({
  badge,
  title,
  description,
  children,
  className,
  ...props
}: ComponentProps<typeof Card> & {
  badge?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <Card className={cn("mr-auto w-full max-w-xl", className)} {...props}>
      <CardHeader>
        {badge}
        <CardTitle className="text-balance">{title}</CardTitle>
        {description ? <CardDescription className="text-pretty">{description}</CardDescription> : null}
      </CardHeader>
      {children ? <CardContent>{children}</CardContent> : null}
    </Card>
  );
}
