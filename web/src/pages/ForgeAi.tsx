import { Navigate, useLocation } from "react-router-dom";

/** The ForgeAI chat moved to Investigations › Ask ForgeAI; old links (and ?chat=…) still land there. */
export function ForgeAiPage() {
  const { search } = useLocation();
  return <Navigate to={{ pathname: "/investigations", search, hash: "#ask-forgeai" }} replace />;
}
