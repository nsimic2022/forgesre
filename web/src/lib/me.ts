import { useEffect, useState } from "react";
import { getJson, type Me } from "./api";

// One /api/v1/me per page load, shared by every component that asks for the role.
let cached: Promise<Me | null> | null = null;
const RANK: Record<string, number> = { viewer: 0, analyst: 1, engineer: 2, admin: 3 };

/** The signed-in user's role rank: 0 viewer, 1 analyst, 2 engineer, 3 admin (-1 while loading). */
export function useRoleRank(): number {
  const [rank, setRank] = useState(-1);
  useEffect(() => {
    cached ??= getJson<Me>("/api/v1/me").catch(() => null);
    let alive = true;
    void cached.then((me) => alive && setRank(RANK[me?.role ?? "viewer"] ?? 0));
    return () => void (alive = false);
  }, []);
  return rank;
}

/** The signed-in person's email ("" until known). */
export function useMyEmail(): string {
  const [email, setEmail] = useState("");
  useEffect(() => {
    cached ??= getJson<Me>("/api/v1/me").catch(() => null);
    let alive = true;
    void cached.then((me) => alive && setEmail(me?.email ?? ""));
    return () => void (alive = false);
  }, []);
  return email;
}
