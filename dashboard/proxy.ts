import { NextResponse, type NextRequest } from "next/server";

// HTTP Basic auth for the whole dashboard (*.vercel.app URLs are public).
// If DASHBOARD_PASSWORD is unset, auth is skipped only outside production.
export function proxy(request: NextRequest) {
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password) {
    if (process.env.NODE_ENV === "production") {
      return new NextResponse("DASHBOARD_PASSWORD is not configured", { status: 503 });
    }
    return NextResponse.next();
  }
  const user = process.env.DASHBOARD_USER || "admin";
  const header = request.headers.get("authorization") || "";
  const [scheme, encoded] = header.split(" ");
  if (scheme === "Basic" && encoded) {
    const [u, ...rest] = atob(encoded).split(":");
    if (u === user && rest.join(":") === password) return NextResponse.next();
  }
  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="papertrade", charset="UTF-8"' },
  });
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
