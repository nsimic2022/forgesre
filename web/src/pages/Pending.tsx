import { Card } from "../components/ui";

// Placeholder for pages that are not rebuilt yet; points at the classic page that works today.
export function Pending({ title, classic }: { title: string; classic: string }) {
  return (
    <>
      <header className="page-head">
        <h1>{title}</h1>
      </header>
      <Card>
        <p style={{ margin: 0 }}>
          This page is being rebuilt to the new design. Until then, use the{" "}
          <a href={classic}>classic {title} page</a>.
        </p>
      </Card>
    </>
  );
}
