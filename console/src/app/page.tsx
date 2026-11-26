import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { Console } from "@/components/Console";
import { COOKIE, fromLan, verifyToken } from "@/lib/auth";

export const dynamic = "force-dynamic";

export default async function Page() {
  // second guard behind proxy.ts: outside the LAN the page never renders without a valid session
  if (!fromLan(await headers()) && !verifyToken((await cookies()).get(COOKIE)?.value)) redirect("/login");
  return <Console />;
}
