export default function ErrorCard({ message }: { message: string }) {
  return (
    <div className="card error" role="alert">
      <h2>Cannot load data</h2>
      <p>{message}</p>
      <p className="muted">Check that the engine is running and ENGINE_API_URL / ENGINE_API_TOKEN are set for the dashboard.</p>
    </div>
  );
}
