import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Link, Lock, Share2, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { call, client } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Label, Select } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { formatDateTime } from "@/lib/utils";

async function copyLink(url: string) {
  try {
    await navigator.clipboard.writeText(url);
    toast.success("Link copied");
  } catch {
    toast.error("Could not copy. Select the link and copy it manually.");
  }
}

export function ShareDialog({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}><Share2 /> Share</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85vh] max-w-xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Share document</DialogTitle>
            <DialogDescription>
              Share a read-only view and downloads. Recipients do not need a DocNest account.
            </DialogDescription>
          </DialogHeader>
          {open && <ShareLinks id={id} />}
        </DialogContent>
      </Dialog>
    </>
  );
}

function ShareLinks({ id }: { id: string }) {
  const qc = useQueryClient();
  const [protection, setProtection] = useState("link");
  const [passphrase, setPassphrase] = useState("");
  const [allowOriginal, setAllowOriginal] = useState(false);
  const [allowText, setAllowText] = useState(true);
  const key = ["document-shares", id];
  const shares = useQuery({
    queryKey: key,
    queryFn: () => call(() => client.GET("/api/v1/documents/{doc_id}/shares", { params: { path: { doc_id: id } } })),
  });
  const create = useMutation({
    mutationFn: () => call(() => client.POST("/api/v1/documents/{doc_id}/shares", {
      params: { path: { doc_id: id } },
      body: { passphrase: protection === "passphrase" ? passphrase : "", allow_original: allowOriginal, allow_text: allowText },
    })),
    onSuccess: () => {
      setPassphrase("");
      void qc.invalidateQueries({ queryKey: key });
      toast.success("Share link created");
    },
  });
  const revoke = useMutation({
    mutationFn: (shareId: string) => call(() => client.DELETE("/api/v1/documents/{doc_id}/shares/{share_id}", {
      params: { path: { doc_id: id, share_id: shareId } },
    })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: key });
      toast.success("Link revoked");
    },
  });
  return (
    <div className="flex flex-col gap-5">
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <Label htmlFor="share-protection">Access</Label>
        <Select id="share-protection" value={protection} onChange={(e) => { setProtection(e.target.value); setPassphrase(""); }}>
          <option value="link">Anyone with the link</option>
          <option value="passphrase">Link and passphrase</option>
        </Select>
        {protection === "passphrase" && (
          <div className="flex flex-col gap-2">
            <Label htmlFor="share-passphrase">Passphrase</Label>
            <Input id="share-passphrase" type="password" autoComplete="new-password" required maxLength={1024}
              value={passphrase} onChange={(e) => setPassphrase(e.target.value)} />
            <p className="text-xs text-muted-foreground">Send the passphrase separately from the link.</p>
          </div>
        )}
        <Label className="flex items-center gap-2 leading-5">
          <Checkbox checked={allowOriginal} onCheckedChange={(v) => setAllowOriginal(v === true)} />
          Allow viewing and downloading the original
        </Label>
        <Label className="flex items-center gap-2 leading-5">
          <Checkbox checked={allowText} onCheckedChange={(v) => setAllowText(v === true)} />
          Include OCR text
        </Label>
        <p className="text-xs text-muted-foreground">
          Links show the current document. The original and OCR text can include pages hidden in Enhanced.
        </p>
        <ErrorNote error={create.error} />
        <Button type="submit" disabled={create.isPending || (protection === "passphrase" && !passphrase.trim())}>
          {create.isPending ? <Spinner /> : <Share2 />} Create share link
        </Button>
      </form>
      <div className="flex flex-col gap-3 border-t pt-4">
        <h3 className="text-sm font-semibold">Active links</h3>
        {shares.isPending && <Spinner />}
        <ErrorNote error={shares.error ?? revoke.error} />
        {shares.data?.length === 0 && <p className="text-sm text-muted-foreground">No share links yet.</p>}
        {shares.data?.map((share) => (
          <div key={share.id} className="flex flex-col gap-2 rounded-md border p-3">
            <div className="flex items-center justify-between gap-2 text-sm">
              <span className="inline-flex items-center gap-1.5 font-medium">
                {share.protected ? <Lock className="size-3.5" /> : <Link className="size-3.5" />}
                {share.protected ? "Passphrase protected" : "Anyone with the link"}
              </span>
              <Button size="sm" variant="ghost" disabled={revoke.isPending} onClick={() => revoke.mutate(share.id)}>
                <Trash2 /> Revoke
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {formatDateTime(share.created_at)} · {share.allow_original ? "Original included" : "Enhanced PDF only"}
              {share.allow_text && " · OCR text included"}
            </p>
            <Label htmlFor={`share-link-${share.id}`} className="text-xs">Viewing link</Label>
            <div className="flex gap-2">
              <Input id={`share-link-${share.id}`} readOnly value={share.url} onFocus={(e) => e.target.select()} />
              <Button variant="outline" size="icon" title="Copy viewing link" onClick={() => void copyLink(share.url)}><Copy /></Button>
            </div>
            <Label htmlFor={`share-pdf-${share.id}`} className="text-xs">Direct enhanced-PDF link</Label>
            <div className="flex gap-2">
              <Input id={`share-pdf-${share.id}`} readOnly value={share.pdf_url} onFocus={(e) => e.target.select()} />
              <Button variant="outline" size="icon" title="Copy PDF link" onClick={() => void copyLink(share.pdf_url)}><Copy /></Button>
            </div>
            {share.protected && <p className="text-xs text-muted-foreground">The PDF link asks for the passphrase in the browser first.</p>}
          </div>
        ))}
      </div>
    </div>
  );
}
