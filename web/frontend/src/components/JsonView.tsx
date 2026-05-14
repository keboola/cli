export function JsonView({ data, maxHeight = "60vh" }: { data: unknown; maxHeight?: string }) {
  return (
    <pre className="nerd-code" style={{ maxHeight, overflow: "auto" }}>
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}
