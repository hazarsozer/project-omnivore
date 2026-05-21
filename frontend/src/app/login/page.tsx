"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Database, Loader2 } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, APIError } from "@/lib/api";
import { setStoredJWT } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!apiKey.trim() || loading) return;

    setLoading(true);
    setError(null);

    try {
      const data = await api.exchangeToken(apiKey.trim());
      setStoredJWT(data.access_token);
      router.replace("/documents");
    } catch (err) {
      const msg =
        err instanceof APIError
          ? err.detail ?? err.message
          : String(err);
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <div className="flex items-center gap-2 mb-1">
            <Database className="h-5 w-5 text-primary" />
            <CardTitle className="text-xl">Omnivore Admin</CardTitle>
          </div>
          <CardDescription>
            Enter your API key to access the dashboard.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="api-key">API Key</Label>
              <Input
                id="api-key"
                type="password"
                placeholder="omni_…"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                autoFocus
                autoComplete="current-password"
              />
            </div>

            {error && (
              <p className="text-sm text-destructive" role="alert">
                {error}
              </p>
            )}

            <Button
              type="submit"
              className="w-full"
              disabled={!apiKey.trim() || loading}
            >
              {loading && <Loader2 className="animate-spin" />}
              Sign In
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
