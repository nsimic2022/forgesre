import { Link } from "react-router-dom";
import { Card } from "../components/ui";

/** Unknown address inside the app. */
export function NotFound() {
  return (
    <>
      <header className="page-head">
        <h1>Not found</h1>
      </header>
      <Card>
        <p style={{ margin: 0 }}>
          There is no page at this address. Go to the <Link to="/">NOC overview</Link>.
        </p>
      </Card>
    </>
  );
}
