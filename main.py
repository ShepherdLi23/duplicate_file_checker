import csv
import hashlib
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections import defaultdict
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "重复文件检测工具"


def resource_path(relative_path):
    """兼容 PyCharm 直接运行与 PyInstaller --onefile 打包后的资源路径。"""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)



class DuplicateFileChecker(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)

        try:
            self.iconbitmap(resource_path("duplicate_file_checker.ico"))
        except Exception:
            # 图标加载失败不影响程序主体功能
            pass

        self.geometry("1100x700")
        self.minsize(900, 560)

        self.folder_var = tk.StringVar()
        self.algorithm_var = tk.StringVar(value="MD5")
        self.recursive_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="请选择要检测的文件夹")
        self.progress_var = tk.DoubleVar(value=0)

        self.result_groups = []
        self.worker_thread = None
        self.cancel_event = threading.Event()
        self.message_queue = queue.Queue()

        self._build_ui()
        self.after(100, self._process_queue)

    def _build_ui(self):
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        ttk.Label(top, text="文件夹：").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        folder_entry = ttk.Entry(top, textvariable=self.folder_var)
        folder_entry.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Button(top, text="浏览...", command=self.choose_folder).grid(
            row=0, column=2, padx=(8, 0), pady=4
        )

        ttk.Label(top, text="哈希算法：").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=4
        )

        algorithm_box = ttk.Combobox(
            top,
            textvariable=self.algorithm_var,
            values=("MD5", "SHA-1", "SHA-256"),
            state="readonly",
            width=12,
        )
        algorithm_box.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Checkbutton(
            top,
            text="包含子文件夹（递归扫描）",
            variable=self.recursive_var,
        ).grid(row=1, column=1, sticky="e", pady=4)

        button_frame = ttk.Frame(top)
        button_frame.grid(row=1, column=2, sticky="e", padx=(8, 0))

        self.scan_button = ttk.Button(
            button_frame, text="开始检测", command=self.start_scan
        )
        self.scan_button.pack(side="left")

        self.cancel_button = ttk.Button(
            button_frame, text="取消", command=self.cancel_scan, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(8, 0))

        top.columnconfigure(1, weight=1)

        progress_frame = ttk.Frame(self, padding=(12, 0, 12, 8))
        progress_frame.pack(fill="x")

        self.progress = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            mode="determinate",
        )
        self.progress.pack(fill="x")

        ttk.Label(progress_frame, textvariable=self.status_var).pack(
            anchor="w", pady=(4, 0)
        )

        result_frame = ttk.Frame(self, padding=(12, 0, 12, 8))
        result_frame.pack(fill="both", expand=True)

        columns = ("group", "size", "hash", "path")
        self.tree = ttk.Treeview(
            result_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
        )

        self.tree.heading("group", text="重复组")
        self.tree.heading("size", text="文件大小")
        self.tree.heading("hash", text="哈希值")
        self.tree.heading("path", text="文件路径")

        self.tree.column("group", width=80, anchor="center", stretch=False)
        self.tree.column("size", width=110, anchor="e", stretch=False)
        self.tree.column("hash", width=280, anchor="w", stretch=False)
        self.tree.column("path", width=560, anchor="w")

        y_scroll = ttk.Scrollbar(
            result_frame, orient="vertical", command=self.tree.yview
        )
        x_scroll = ttk.Scrollbar(
            result_frame, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)

        bottom = ttk.Frame(self, padding=(12, 0, 12, 12))
        bottom.pack(fill="x")

        self.summary_label = ttk.Label(bottom, text="尚未检测")
        self.summary_label.pack(side="left")

        ttk.Button(
            bottom,
            text="导出结果 CSV",
            command=self.export_csv,
        ).pack(side="right")

        ttk.Button(
            bottom,
            text="删除选中文件",
            command=self.delete_selected_files,
        ).pack(side="right", padx=(0, 8))

        ttk.Button(
            bottom,
            text="打开文件所在位置",
            command=self.open_selected_location,
        ).pack(side="right", padx=(0, 8))

        # 双击某一行也可以打开该文件所在位置
        self.tree.bind("<Double-1>", lambda event: self.open_selected_location())

    def choose_folder(self):
        folder = filedialog.askdirectory(title="选择要检测的文件夹")
        if folder:
            self.folder_var.set(folder)

    def start_scan(self):
        folder = self.folder_var.get().strip()

        if not folder:
            messagebox.showwarning("提示", "请先选择一个文件夹。")
            return

        if not os.path.isdir(folder):
            messagebox.showerror("错误", "所选路径不是有效文件夹。")
            return

        if self.worker_thread and self.worker_thread.is_alive():
            return

        self.cancel_event.clear()
        self.result_groups = []
        self._clear_tree()

        self.progress_var.set(0)
        self.summary_label.config(text="正在扫描...")
        self.status_var.set("正在统计文件...")
        self.scan_button.config(state="disabled")
        self.cancel_button.config(state="normal")

        algorithm = self.algorithm_var.get()
        recursive = self.recursive_var.get()

        self.worker_thread = threading.Thread(
            target=self._scan_worker,
            args=(folder, algorithm, recursive),
            daemon=True,
        )
        self.worker_thread.start()

    def cancel_scan(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.cancel_event.set()
            self.status_var.set("正在取消...")

    def _scan_worker(self, folder, algorithm, recursive):
        try:
            files = self._collect_files(folder, recursive)

            if self.cancel_event.is_set():
                self.message_queue.put(("cancelled",))
                return

            total_files = len(files)
            self.message_queue.put(("status", f"共发现 {total_files} 个文件，正在按文件大小分组..."))

            size_groups = defaultdict(list)

            for index, path in enumerate(files, start=1):
                if self.cancel_event.is_set():
                    self.message_queue.put(("cancelled",))
                    return

                try:
                    size = os.path.getsize(path)
                    size_groups[size].append(path)
                except (OSError, PermissionError):
                    pass

                progress = (index / max(total_files, 1)) * 20
                self.message_queue.put(("progress", progress))

            candidates = []
            for size, paths in size_groups.items():
                if len(paths) > 1:
                    for path in paths:
                        candidates.append((size, path))

            candidate_count = len(candidates)

            if candidate_count == 0:
                self.message_queue.put(("done", [], total_files, 0, 0))
                return

            hash_groups = defaultdict(list)

            for index, (size, path) in enumerate(candidates, start=1):
                if self.cancel_event.is_set():
                    self.message_queue.put(("cancelled",))
                    return

                try:
                    digest = self._calculate_hash(path, algorithm)
                    hash_groups[(size, digest)].append(path)
                except (OSError, PermissionError):
                    continue

                progress = 20 + (index / max(candidate_count, 1)) * 80
                self.message_queue.put(
                    ("progress", progress)
                )
                self.message_queue.put(
                    ("status", f"正在计算哈希：{index}/{candidate_count}  {os.path.basename(path)}")
                )

            duplicates = []
            duplicate_file_count = 0
            duplicate_bytes = 0

            for (size, digest), paths in hash_groups.items():
                if len(paths) > 1:
                    duplicates.append(
                        {
                            "size": size,
                            "hash": digest,
                            "paths": sorted(paths, key=str.lower),
                        }
                    )
                    duplicate_file_count += len(paths)
                    # 每组保留一个文件，其余视为可释放空间
                    duplicate_bytes += size * (len(paths) - 1)

            duplicates.sort(
                key=lambda item: (item["size"] * (len(item["paths"]) - 1)),
                reverse=True,
            )

            self.message_queue.put(
                (
                    "done",
                    duplicates,
                    total_files,
                    duplicate_file_count,
                    duplicate_bytes,
                )
            )

        except Exception as exc:
            self.message_queue.put(("error", str(exc)))

    def _collect_files(self, folder, recursive):
        files = []

        if recursive:
            for root, _, filenames in os.walk(folder):
                if self.cancel_event.is_set():
                    break
                for name in filenames:
                    files.append(os.path.join(root, name))
        else:
            try:
                for name in os.listdir(folder):
                    path = os.path.join(folder, name)
                    if os.path.isfile(path):
                        files.append(path)
            except OSError:
                pass

        return files

    @staticmethod
    def _calculate_hash(path, algorithm):
        algorithm_map = {
            "MD5": "md5",
            "SHA-1": "sha1",
            "SHA-256": "sha256",
        }

        hasher = hashlib.new(algorithm_map[algorithm])

        # 分块读取，避免大文件一次性加载到内存
        with open(path, "rb") as file:
            while True:
                block = file.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)

        return hasher.hexdigest()

    def _process_queue(self):
        try:
            while True:
                message = self.message_queue.get_nowait()
                msg_type = message[0]

                if msg_type == "status":
                    self.status_var.set(message[1])

                elif msg_type == "progress":
                    self.progress_var.set(message[1])

                elif msg_type == "done":
                    _, duplicates, total_files, duplicate_count, duplicate_bytes = message
                    self.result_groups = duplicates
                    self._show_results()

                    self.progress_var.set(100)
                    self.status_var.set("检测完成")
                    self.summary_label.config(
                        text=(
                            f"扫描 {total_files} 个文件；"
                            f"发现 {len(duplicates)} 组重复文件，"
                            f"共 {duplicate_count} 个重复项；"
                            f"理论可释放空间约 {self._format_size(duplicate_bytes)}"
                        )
                    )

                    self.scan_button.config(state="normal")
                    self.cancel_button.config(state="disabled")

                elif msg_type == "cancelled":
                    self.status_var.set("检测已取消")
                    self.summary_label.config(text="检测已取消")
                    self.scan_button.config(state="normal")
                    self.cancel_button.config(state="disabled")

                elif msg_type == "error":
                    self.status_var.set("检测失败")
                    self.summary_label.config(text="检测失败")
                    self.scan_button.config(state="normal")
                    self.cancel_button.config(state="disabled")
                    messagebox.showerror("错误", message[1])

        except queue.Empty:
            pass

        self.after(100, self._process_queue)

    def _show_results(self):
        self._clear_tree()

        for group_no, group in enumerate(self.result_groups, start=1):
            size_text = self._format_size(group["size"])
            digest = group["hash"]

            for path in group["paths"]:
                self.tree.insert(
                    "",
                    "end",
                    values=(group_no, size_text, digest, path),
                )

    def _clear_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

    def _get_selected_paths(self):
        selected_items = self.tree.selection()
        paths = []

        for item_id in selected_items:
            values = self.tree.item(item_id, "values")
            if len(values) >= 4:
                paths.append(values[3])

        return paths

    def open_selected_location(self):
        paths = self._get_selected_paths()

        if not paths:
            messagebox.showinfo("提示", "请先在结果列表中选择一个文件。")
            return

        # 一次只打开第一个选中文件，避免批量弹出大量资源管理器窗口
        path = paths[0]

        if not os.path.exists(path):
            messagebox.showerror("错误", f"文件不存在：\n{path}")
            return

        try:
            if sys.platform.startswith("win"):
                # Windows：打开资源管理器并选中文件
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                # macOS：在 Finder 中显示文件
                subprocess.Popen(["open", "-R", path])
            else:
                # Linux：打开所在目录
                folder = os.path.dirname(path)
                subprocess.Popen(["xdg-open", folder])

        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def delete_selected_files(self):
        selected_items = self.tree.selection()

        if not selected_items:
            messagebox.showinfo("提示", "请先选择要删除的重复文件。")
            return

        selected_paths = []
        selected_by_group = defaultdict(list)

        for item_id in selected_items:
            values = self.tree.item(item_id, "values")
            if len(values) < 4:
                continue

            group_no = int(values[0])
            path = values[3]
            selected_paths.append(path)
            selected_by_group[group_no].append(path)

        if not selected_paths:
            return

        # 安全检查：不允许把某个重复组的所有文件一次性全部删除
        for group_no, paths in selected_by_group.items():
            group_index = group_no - 1

            if 0 <= group_index < len(self.result_groups):
                current_group_paths = [
                    p for p in self.result_groups[group_index]["paths"]
                    if os.path.exists(p)
                ]

                selected_existing = [
                    p for p in paths
                    if os.path.exists(p)
                ]

                if current_group_paths and len(selected_existing) >= len(current_group_paths):
                    messagebox.showwarning(
                        "安全限制",
                        (
                            f"重复组 {group_no} 中的所有文件都被选中了。\n\n"
                            "程序不会一次性删除同一重复组的全部副本，"
                            "请至少保留一个文件。"
                        ),
                    )
                    return

        preview_limit = 8
        preview = "\n".join(selected_paths[:preview_limit])

        if len(selected_paths) > preview_limit:
            preview += f"\n……另外还有 {len(selected_paths) - preview_limit} 个文件"

        confirm = messagebox.askyesno(
            "确认删除",
            (
                f"确定要永久删除选中的 {len(selected_paths)} 个文件吗？\n\n"
                f"{preview}\n\n"
                "此操作会直接删除文件，不会移动到回收站，且无法由本程序恢复。"
            ),
            icon="warning",
        )

        if not confirm:
            return

        deleted = []
        failed = []

        for path in selected_paths:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    deleted.append(path)
                else:
                    failed.append((path, "文件不存在或不是普通文件"))
            except Exception as exc:
                failed.append((path, str(exc)))

        if deleted:
            self._refresh_results_after_delete(deleted)

        if failed:
            failed_preview = "\n".join(
                f"{path}\n  原因：{reason}"
                for path, reason in failed[:5]
            )

            if len(failed) > 5:
                failed_preview += f"\n……另外还有 {len(failed) - 5} 个删除失败"

            messagebox.showwarning(
                "部分文件删除失败",
                (
                    f"成功删除 {len(deleted)} 个文件；"
                    f"失败 {len(failed)} 个文件。\n\n"
                    f"{failed_preview}"
                ),
            )
        else:
            messagebox.showinfo(
                "删除完成",
                f"已成功删除 {len(deleted)} 个文件。",
            )

    def _refresh_results_after_delete(self, deleted_paths):
        deleted_set = set(deleted_paths)
        refreshed_groups = []

        for group in self.result_groups:
            remaining_paths = [
                path for path in group["paths"]
                if path not in deleted_set and os.path.exists(path)
            ]

            # 少于两个文件后，该组已经不再是“重复文件组”
            if len(remaining_paths) > 1:
                refreshed_groups.append(
                    {
                        "size": group["size"],
                        "hash": group["hash"],
                        "paths": remaining_paths,
                    }
                )

        self.result_groups = refreshed_groups
        self._show_results()

        duplicate_file_count = sum(
            len(group["paths"]) for group in self.result_groups
        )

        duplicate_bytes = sum(
            group["size"] * (len(group["paths"]) - 1)
            for group in self.result_groups
        )

        if self.result_groups:
            self.summary_label.config(
                text=(
                    f"当前剩余 {len(self.result_groups)} 组重复文件，"
                    f"共 {duplicate_file_count} 个重复项；"
                    f"理论可释放空间约 {self._format_size(duplicate_bytes)}"
                )
            )
        else:
            self.summary_label.config(text="当前已无重复文件组")

    def export_csv(self):
        if not self.result_groups:
            messagebox.showinfo("提示", "当前没有可导出的重复文件结果。")
            return

        output_path = filedialog.asksaveasfilename(
            title="导出重复文件结果",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            initialfile="duplicate_files.csv",
        )

        if not output_path:
            return

        try:
            with open(output_path, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow(["重复组", "文件大小(Byte)", "文件大小", "哈希值", "文件路径"])

                for group_no, group in enumerate(self.result_groups, start=1):
                    for path in group["paths"]:
                        writer.writerow(
                            [
                                group_no,
                                group["size"],
                                self._format_size(group["size"]),
                                group["hash"],
                                path,
                            ]
                        )

            messagebox.showinfo("完成", f"结果已导出到：\n{output_path}")

        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))

    @staticmethod
    def _format_size(size):
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)

        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)} {unit}"
                return f"{value:.2f} {unit}"
            value /= 1024

        return f"{size} B"


if __name__ == "__main__":
    app = DuplicateFileChecker()
    app.mainloop()
