"use client";

import { useState } from "react";
import { toast } from "sonner";
import {
  AlertCircle,
  Check,
  ChevronsUpDown,
  Info,
  Settings,
  Sparkles,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { MermaidDiagram } from "@/components/mermaid-diagram";

const MERMAID_SAMPLE = `flowchart LR
  A[Producer Chat] --> B[Pipeline]
  B --> C{Gatekeeper}
  C -->|pass| D[Render]
  C -->|fail| E[Replan]`;

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-3 rounded-lg border bg-card p-5 text-card-foreground shadow-sm">
      <h2 className="text-sm font-semibold tracking-tight text-muted-foreground">
        {title}
      </h2>
      <div className="flex flex-wrap items-start gap-3">{children}</div>
    </section>
  );
}

export default function UiKitPage() {
  const [collapsibleOpen, setCollapsibleOpen] = useState(false);

  return (
    <TooltipProvider>
      <main className="mx-auto min-h-screen max-w-5xl space-y-6 bg-background p-8 text-foreground">
        <header className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            UI Kit — shadcn/ui primitives
          </h1>
          <p className="text-sm text-muted-foreground">
            Sanity page showing each primitive wired up with the repo&apos;s
            neutral theme. Children issues migrate real components.
          </p>
        </header>

        <Section title="Button">
          <Button>Default</Button>
          <Button variant="secondary">Secondary</Button>
          <Button variant="destructive">Destructive</Button>
          <Button variant="outline">Outline</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="link">Link</Button>
          <Button size="sm">
            <Sparkles className="mr-1 h-4 w-4" /> Small
          </Button>
          <Button size="icon" aria-label="settings">
            <Settings className="h-4 w-4" />
          </Button>
        </Section>

        <Section title="Card">
          <Card className="w-72">
            <CardHeader>
              <CardTitle>Restated brief</CardTitle>
              <CardDescription>What the producer heard.</CardDescription>
            </CardHeader>
            <CardContent className="text-sm">
              Two scenes, ~60s, synthetic media, dual language.
            </CardContent>
            <CardFooter className="justify-end">
              <Button size="sm" variant="outline">
                Edit
              </Button>
            </CardFooter>
          </Card>
        </Section>

        <Section title="Badge">
          <Badge>live</Badge>
          <Badge variant="secondary">queued</Badge>
          <Badge variant="destructive">failed</Badge>
          <Badge variant="outline">draft</Badge>
        </Section>

        <Section title="Progress">
          <div className="w-72 space-y-2">
            <Progress value={34} />
            <Progress value={72} />
          </div>
        </Section>

        <Section title="Input">
          <Input placeholder="topic…" className="w-72" />
        </Section>

        <Section title="Tabs">
          <Tabs defaultValue="scenario" className="w-[28rem]">
            <TabsList>
              <TabsTrigger value="scenario">Scenario</TabsTrigger>
              <TabsTrigger value="clips">Clips</TabsTrigger>
              <TabsTrigger value="timeline">Timeline</TabsTrigger>
            </TabsList>
            <TabsContent value="scenario" className="pt-3 text-sm">
              Scenario editor tab content.
            </TabsContent>
            <TabsContent value="clips" className="pt-3 text-sm">
              Clip reviewer tab content.
            </TabsContent>
            <TabsContent value="timeline" className="pt-3 text-sm">
              Timeline view tab content.
            </TabsContent>
          </Tabs>
        </Section>

        <Section title="Collapsible">
          <Collapsible
            open={collapsibleOpen}
            onOpenChange={setCollapsibleOpen}
            className="w-72 space-y-2"
          >
            <div className="flex items-center justify-between">
              <span className="text-sm">Reasoning trace</span>
              <CollapsibleTrigger asChild>
                <Button variant="ghost" size="icon" aria-label="toggle">
                  <ChevronsUpDown className="h-4 w-4" />
                </Button>
              </CollapsibleTrigger>
            </div>
            <CollapsibleContent className="rounded-md border p-3 text-xs text-muted-foreground">
              Step 1 — scout corpus. Step 2 — draft scenario. Step 3 — QA.
            </CollapsibleContent>
          </Collapsible>
        </Section>

        <Section title="Tooltip + HoverCard">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="outline" size="icon" aria-label="info">
                <Info className="h-4 w-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Short hint copy.</TooltipContent>
          </Tooltip>
          <HoverCard>
            <HoverCardTrigger asChild>
              <Button variant="outline">Hover me</Button>
            </HoverCardTrigger>
            <HoverCardContent className="w-64 text-sm">
              Deeper preview card — used for slot details and speaker bios.
            </HoverCardContent>
          </HoverCard>
        </Section>

        <Section title="Dialog + AlertDialog + Sheet">
          <Dialog>
            <DialogTrigger asChild>
              <Button variant="outline">Open dialog</Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Preview clip</DialogTitle>
                <DialogDescription>
                  Inspect the generated clip before approving.
                </DialogDescription>
              </DialogHeader>
            </DialogContent>
          </Dialog>
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="destructive">
                <AlertCircle className="mr-1 h-4 w-4" /> Halt production
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Halt production?</AlertDialogTitle>
                <AlertDialogDescription>
                  This stops the current run. Progress will be checkpointed.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction>Halt</AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
          <Sheet>
            <SheetTrigger asChild>
              <Button variant="outline">Open drilldown</Button>
            </SheetTrigger>
            <SheetContent side="right">
              <SheetHeader>
                <SheetTitle>Slot detail</SheetTitle>
                <SheetDescription>
                  Per-slot logs, artifacts, and QA results.
                </SheetDescription>
              </SheetHeader>
            </SheetContent>
          </Sheet>
        </Section>

        <Section title="DropdownMenu">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline">Control menu</Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuLabel>Run</DropdownMenuLabel>
              <DropdownMenuItem>Pause production</DropdownMenuItem>
              <DropdownMenuItem>Replan scenario</DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem>Export OTIO</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </Section>

        <Section title="Command">
          <Command className="w-72 rounded-md border">
            <CommandInput placeholder="Search actions…" />
            <CommandList>
              <CommandEmpty>No results.</CommandEmpty>
              <CommandGroup heading="Run">
                <CommandItem>
                  <Check className="mr-2 h-4 w-4" /> Approve scenario
                </CommandItem>
                <CommandItem>
                  <Check className="mr-2 h-4 w-4" /> Regenerate clip
                </CommandItem>
              </CommandGroup>
            </CommandList>
          </Command>
        </Section>

        <Section title="Sonner toasts">
          <Button
            variant="outline"
            onClick={() => toast.success("Scenario approved")}
          >
            Trigger toast
          </Button>
        </Section>

        <Section title="Mermaid diagram">
          <div className="w-full max-w-3xl rounded-md border bg-white p-4 dark:bg-neutral-900">
            <MermaidDiagram chart={MERMAID_SAMPLE} />
          </div>
        </Section>
      </main>
    </TooltipProvider>
  );
}
