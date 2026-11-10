import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

export function PublicNameRequestPage() {
  const params = new URLSearchParams(window.location.search);
  const referral = params.get("ref") || "";
  const [email, setEmail] = useState("");
  const [style, setStyle] = useState("");
  const [consent, setConsent] = useState(false);
  const [verified, setVerified] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const verifyToken = params.get("token") || "";
  const verify = useMutation({
    mutationFn: () => api.verifyPublicNameRequest(verifyToken),
    onSuccess: (result) => { setVerified(true); setMessage(result.message); },
    onError: (err) => setError((err as Error).message),
  });
  const submit = useMutation({
    mutationFn: () => api.submitPublicNameRequest({
      email,
      consent,
      source: "website",
      referral_code: referral,
      preferred_style: style,
    }),
    onSuccess: (result) => { setMessage(result.message); setError(""); },
    onError: (err) => setError((err as Error).message),
  });
  if (verifyToken) {
    return <div className="max-w-xl mx-auto py-16"><Card><CardHeader><CardTitle>Confirm your Chinese name request</CardTitle></CardHeader><CardContent className="space-y-4"><p>{verified ? message : "Confirm your email address to receive updates about your Chinese-name request."}</p>{error && <p className="text-sm text-destructive">{error}</p>}{!verified && <Button onClick={() => verify.mutate()} disabled={verify.isPending}>{verify.isPending ? "Confirming..." : "Confirm email"}</Button>}</CardContent></Card></div>;
  }
  return <div className="max-w-xl mx-auto py-16"><Card><CardHeader><CardTitle>Find your Chinese name</CardTitle><p className="text-sm text-muted-foreground">Tell us where to send your personalized Chinese-name suggestions.</p></CardHeader><CardContent className="space-y-4">
    <div><label className="text-sm font-medium">Email address</label><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" /></div>
    <div><label className="text-sm font-medium">What style or meaning do you like? (optional)</label><Input value={style} onChange={(e) => setStyle(e.target.value)} placeholder="e.g. wisdom, nature, kindness, elegant" /></div>
    <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} /><span>I agree to receive emails about my Chinese-name request and related service follow-up. I can unsubscribe at any time.</span></label>
    <p className="text-xs text-muted-foreground">We will send one confirmation email first. No follow-up emails will be sent until you confirm your address.</p>
    {message && <p className="text-sm text-green-700">{message}</p>}
    {error && <p className="text-sm text-destructive">{error}</p>}
    <Button className="w-full" disabled={!email || !consent || submit.isPending} onClick={() => submit.mutate()}>{submit.isPending ? "Sending confirmation..." : "Submit and confirm by email"}</Button>
  </CardContent></Card></div>;
}
