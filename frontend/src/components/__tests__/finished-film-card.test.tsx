/** @jest-environment jsdom */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FinishedFilmCard } from "@/components/finished-film-card";
import type { FinishedFilm } from "@/lib/types";

function makeFilm(overrides: Partial<FinishedFilm> = {}): FinishedFilm {
  return {
    url: "/agui/final_film/final_documentary.mp4",
    duration_sec: 420,
    language: "",
    alternates: [],
    ...overrides,
  };
}

describe("FinishedFilmCard (UX-01)", () => {
  it("renders the card with runtime + watch button when film is ready", () => {
    render(<FinishedFilmCard film={makeFilm()} />);
    expect(screen.getByTestId("finished-film-card")).toBeInTheDocument();
    expect(screen.getByText(/Your documentary is ready/i)).toBeInTheDocument();
    // 420s -> 7:00 runtime
    expect(screen.getByText(/7:00 runtime/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /watch your film/i }),
    ).toBeInTheDocument();
  });

  it("opens and closes the modal player", async () => {
    render(<FinishedFilmCard film={makeFilm()} />);
    expect(screen.queryByTestId("finished-film-modal")).toBeNull();

    await userEvent.click(
      screen.getByRole("button", { name: /watch your film/i }),
    );
    expect(screen.getByTestId("finished-film-modal")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: /close finished film player/i }),
    );
    expect(screen.queryByTestId("finished-film-modal")).toBeNull();
  });

  it("exposes a language selector in dual-language mode", async () => {
    const film = makeFilm({
      language: "ru",
      url: "/agui/final_film/final_documentary_ru.mp4",
      alternates: [
        {
          url: "/agui/final_film/final_documentary_en.mp4",
          duration_sec: 420,
          language: "en",
        },
      ],
    });
    render(<FinishedFilmCard film={film} />);
    const ruBtn = screen.getByRole("button", { name: /^RU$/i });
    const enBtn = screen.getByRole("button", { name: /^EN$/i });
    expect(ruBtn).toHaveAttribute("aria-pressed", "true");
    expect(enBtn).toHaveAttribute("aria-pressed", "false");

    await userEvent.click(enBtn);
    expect(enBtn).toHaveAttribute("aria-pressed", "true");
    expect(ruBtn).toHaveAttribute("aria-pressed", "false");
  });

  it("surfaces a download link that points at the agui route", () => {
    render(<FinishedFilmCard film={makeFilm()} />);
    const dl = screen.getByRole("link", { name: /download finished film/i });
    expect(dl.getAttribute("href")).toContain(
      "/agui/final_film/final_documentary.mp4",
    );
    expect(dl).toHaveAttribute("download");
  });
});
