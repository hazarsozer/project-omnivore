const JWT_KEY = "omnivore_jwt";

export function getStoredJWT(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(JWT_KEY);
}

export function setStoredJWT(token: string): void {
  sessionStorage.setItem(JWT_KEY, token);
}

export function clearAuth(): void {
  sessionStorage.removeItem(JWT_KEY);
}

export function isAuthenticated(): boolean {
  return (
    getStoredJWT() !== null ||
    (typeof process !== "undefined" && !!process.env.NEXT_PUBLIC_API_KEY)
  );
}
