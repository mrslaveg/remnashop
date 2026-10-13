import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "@/auth/AuthContext";
import { Layout } from "@/components/Layout";
import { RequireAuth } from "@/components/RequireAuth";
import { DevicesPage } from "@/pages/DevicesPage";
import { LoginPage } from "@/pages/LoginPage";
import { PlansPage } from "@/pages/PlansPage";
import { SubscriptionPage } from "@/pages/SubscriptionPage";

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            element={
              <RequireAuth>
                <Layout />
              </RequireAuth>
            }
          >
            <Route index element={<SubscriptionPage />} />
            <Route path="plans" element={<PlansPage />} />
            <Route path="devices" element={<DevicesPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
