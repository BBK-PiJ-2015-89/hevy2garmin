import { describe, expect, it } from "vitest";
import { plannedDescription, type PlannedWorkout } from "./planned-strava";

describe("planned Strava descriptions", () => {
  it("formats a generic planned running workout", () => {
    const plan: PlannedWorkout = {
      garminWorkoutId: "123",
      planName: "Half Marathon Plan",
      workoutTitle: "Half Marathon Plan - Training Day 6 - Tempo",
      scheduledDate: "2026-11-01",
      steps: [
        { kind: "step", name: "Warm up", durationType: "distance", durationValue: 2000 },
        {
          kind: "step",
          name: "Tempo",
          durationType: "distance",
          durationValue: 5000,
          targetType: "pace",
          targetLow: "4:45",
          targetHigh: "4:55",
        },
        { kind: "step", name: "Easy", durationType: "time", durationValue: 300 },
        { kind: "repeat", previousSteps: 2, reps: 3 },
      ],
    };

    expect(plannedDescription(plan)).toContain(
      "Half Marathon Plan - Training Day 6 - Tempo",
    );
    expect(plannedDescription(plan)).toContain("Warm up: 2 km");
    expect(plannedDescription(plan)).toContain("Tempo: 5 km @ 4:45-4:55/km");
    expect(plannedDescription(plan)).toContain("Repeat: previous 2 steps x 3");
  });
});
