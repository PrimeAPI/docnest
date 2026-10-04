import { createBrowserRouter, Navigate } from "react-router";
import { AppLayout } from "@/components/layout";
import { AuthGate } from "@/features/auth/auth-gate";
import { LoginPage } from "@/features/auth/login-page";
import { SetupPage } from "@/features/auth/setup-page";
import { DocumentPage } from "@/pages/document-page";
import { DocumentsPage } from "@/pages/documents-page";
import { InboxPage } from "@/pages/inbox-page";
import { OrganizePage } from "@/pages/organize-page";
import { ReviewPage } from "@/pages/review-page";
import { SeriesDetailPage, SeriesPage } from "@/pages/series-page";
import { SettingsPage } from "@/pages/settings-page";
import { TodosPage } from "@/pages/todos-page";

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  { path: "/setup", element: <SetupPage /> },
  {
    element: (
      <AuthGate>
        <AppLayout />
      </AuthGate>
    ),
    children: [
      { index: true, element: <Navigate to="/inbox" replace /> },
      { path: "inbox", element: <InboxPage /> },
      { path: "inbox/review", element: <ReviewPage /> },
      { path: "documents", element: <DocumentsPage /> },
      { path: "documents/:id", element: <DocumentPage /> },
      { path: "todos", element: <TodosPage /> },
      { path: "series", element: <SeriesPage /> },
      { path: "series/:id", element: <SeriesDetailPage /> },
      { path: "organize", element: <OrganizePage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "*", element: <Navigate to="/inbox" replace /> },
    ],
  },
]);
