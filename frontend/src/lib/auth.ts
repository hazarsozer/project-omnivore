const JWT_KEY = "omnivore_jwt";
const API_KEY_KEY = "omnivore_api_key";

export function getStoredJWT(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(JWT_KEY);
}

export function setStoredJWT(token: string): void {
  sessionStorage.setItem(JWT_KEY, token);
}

export function getStoredApiKey(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(API_KEY_KEY);
}

export function setStoredApiKey(key: string): void {
  sessionStorage.setItem(API_KEY_KEY, key);
}

export function clearAuth(): void {
  sessionStorage.removeItem(JWT_KEY);
  sessionStorage.removeItem(API_KEY_KEY);
}

export function isAuthenticated(): boolean {
  return (
    getStoredJWT() !== null ||
    (typeof process !== "undefined" && !!process.env.NEXT_PUBLIC_API_KEY)
  );
}
