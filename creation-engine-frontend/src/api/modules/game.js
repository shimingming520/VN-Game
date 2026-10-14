import request from "@/api/request";

const LOCAL_GAME_BASE_URL = "http://localhost:8088";

/** 游戏库列表：本地模式直接展示已完成且已构建的视觉小说。 */
export async function getLibraryGamesApi(params = {}) {
  const response = await request.get("/v1/tasks", {
    params: {
      limit: 100,
      offset: 0,
      status: 2,
      ...params,
    },
  });
  const data = response?.data ?? response ?? {};
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const items = tasks
    .filter((task) => Number(task.overall_status_code) === 2)
    .map((task) => ({
      id: task.task_id,
      task_id: task.task_id,
      name: task.project_name || task.novel_filename || "未命名作品",
      description: task.novel_filename || "本地生成的视觉小说",
      game_type: "novel",
      url: `${LOCAL_GAME_BASE_URL}/${task.task_id}/`,
      cover: "",
    }));

  return {
    items,
    total: items.length,
  };
}
